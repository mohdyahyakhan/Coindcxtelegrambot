from flask import Flask
import threading
import asyncio
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
import numpy as np

# =============== EDIT THESE CREDENTIALS ===============
TELEGRAM_BOT_TOKEN = "8906533334:AAHI1LT_kPuGex0ved3juNjjgfjuEVFONy0"
TELEGRAM_CHAT_ID = "-5212565182"
# =====================================================

API_TICKERS = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"
API_CANDLES = "https://public.coindcx.com/market_data/candles"
ALERT_THRESHOLD = 40.0
SCAN_INTERVAL = 300
ALERT_COOLDOWN = 21600
WATCHLIST_DAYS = 2 # 2 din tak watch karega

# Supertrend function
def supertrend(high, low, close, period=10, multiplier=3):
    tr1 = high - low
    tr2 = np.abs(high - np.roll(close, 1))
    tr3 = np.abs(low - np.roll(close, 1))
    tr = np.maximum(tr1, np.maximum(tr2, tr3))
    atr = np.convolve(tr, np.ones(period)/period, mode='valid')
    atr = np.concatenate([np.full(period-1, np.nan), atr])

    hl2 = (high + low) / 2
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = np.full(len(close), np.nan)
    direction = np.full(len(close), 1) # 1 = up, -1 = down

    for i in range(1, len(close)):
        if close[i] > upperband[i-1]:
            direction[i] = 1
        elif close[i] < lowerband[i-1]:
            direction[i] = -1
        else:
            direction[i] = direction[i-1]
            if direction[i] == 1 and lowerband[i] < lowerband[i-1]:
                lowerband[i] = lowerband[i-1]
            if direction[i] == -1 and upperband[i] > upperband[i-1]:
                upperband[i] = upperband[i-1]

        if direction[i] == 1:
            supertrend[i] = lowerband[i]
        else:
            supertrend[i] = upperband[i]

    return supertrend, direction

class AlertBot:
    def __init__(self):
        self.session = None
        self.last_alerted = {}
        self.watchlist = {} # {symbol: {'added': timestamp, 'alerted': False}}

    async def send_telegram(self, text: str):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        try:
            async with self.session.post(url, json=payload, timeout=10) as r:
                if r.status!= 200:
                    print(f"Telegram Error {r.status}: {await r.text()}")
        except Exception as e:
            print(f"Failed to send Telegram: {e}")

    async def fetch_perps(self):
        async with self.session.get(API_TICKERS, timeout=15) as r:
            r.raise_for_status()
            data = await r.json()
            return data.get('prices', {})

    async def fetch_candles(self, symbol, interval="5m", limit=400):
        params = {"pair": symbol, "interval": interval, "limit": limit}
        async with self.session.get(API_CANDLES, params=params, timeout=15) as r:
            if r.status!= 200: return None
            data = await r.json()
            if not data or len(data) < 300: return None
            return {
                'high': np.array([float(x['high']) for x in data]),
                'low': np.array([float(x['low']) for x in data]),
                'close': np.array([float(x['close']) for x in data])
            }

    async def check_entry_condition(self, symbol):
        df = await self.fetch_candles(symbol, "5m", 400)
        if df is None: return False, "No Data"

        close, high, low = df['close'], df['high'], df['low']

        # EMA 300
        ema300 = np.convolve(close, np.ones(300)/300, mode='valid')
        ema300 = np.concatenate([np.full(299, np.nan), ema300])

        # Supertrend 10,3
        st, direction = supertrend(high, low, close, 10, 3)

        # Conditions:
        # 1. Supertrend ne EMA300 ko neeche cross kiya
        # 2. Price Supertrend ke neeche hai
        st_below_ema = st[-1] < ema300[-1] and st[-2] >= ema300[-2] # Cross under
        price_below_st = close[-1] < st[-1]

        if st_below_ema and price_below_st:
            return True, f"ST: ${st[-1]:.4f} < EMA300: ${ema300[-1]:.4f}"
        else:
            return False, f"ST: ${st[-1]:.4f} | EMA300: ${ema300[-1]:.4f}"

    async def scan(self):
        try:
            tickers = await self.fetch_perps()
            now = time.time()
            ts = datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S IST')
            print(f"[{ts}] Scanning {len(tickers)} markets | Watchlist: {len(self.watchlist)}")

            # 1. 40%+ Pump Alert - Band nahi hoga
            for symbol, t in tickers.items():
                if not symbol.startswith('B-'): continue

                coin = symbol.replace('B-', '').replace('_USDT', '')
                change_24h = float(t.get('pc', 0))
                price = float(t.get('ls', 0))

                if change_24h >= ALERT_THRESHOLD:
                    last_time = self.last_alerted.get(symbol, 0)
                    if now - last_time > ALERT_COOLDOWN:
                        msg = (
                            f"🚀 *40%+ PUMP ALERT*\n"
                            f"Coin: `{coin}`\n"
                            f"Signal: *24H UP MOVE*\n"
                            f"24h Change: *+{change_24h:.2f}%*\n"
                            f"Price: `${price:,.4f}`\n"
                            f"Time: `{ts}`"
                        )
                        await self.send_telegram(msg)
                        self.last_alerted[symbol] = now
                        # Watchlist me add kar do
                        self.watchlist[symbol] = {'added': now, 'alerted': False}
                        print(f"PUMP Alert sent for {coin} + Added to watchlist")

            # 2. Watchlist Cleanup - 2 din purane hatao
            expired = [s for s, v in self.watchlist.items() if now - v['added'] > WATCHLIST_DAYS * 86400]
            for s in expired: del self.watchlist[s]

            # 3. Entry Condition Check - Watchlist wale coins pe
            for symbol, data in list(self.watchlist.items()):
                if data['alerted']: continue # Ek baar hi confirmation bhejna hai

                coin = symbol.replace('B-', '').replace('_USDT', '')
                entry_ok, entry_info = await self.check_entry_condition(symbol)

                if entry_ok:
                    current_price = float(tickers[symbol]['ls'])
                    msg = (
                        f"🎯 *CONFIRMATION ENTRY ALERT*\n"
                        f"Coin: `{coin}`\n"
                        f"Signal: *ST 10,3 < EMA300 + Price < ST*\n"
                        f"Condition: `{entry_info}`\n"
                        f"Price: `${current_price:,.4f}`\n"
                        f"Timeframe: `5m`\n"
                        f"Time: `{ts}`"
                    )
                    await self.send_telegram(msg)
                    self.watchlist[symbol]['alerted'] = True
                    print(f"CONFIRMATION Alert sent for {coin}")

        except Exception as e:
            print(f"Scan error: {e}")

    async def run(self):
        self.session = aiohttp.ClientSession()
        print("="*60)
        print("CoinDCX Pump + Entry Confirmation Monitor")
        print("="*60)
        print("✅ Bot 1: 40%+ Pump Alert - ACTIVE")
        print("✅ Bot 2: ST+EMA300 Entry - ACTIVE | Watchlist 2 Days")
        print(f"INFO - Threshold: +{ALERT_THRESHOLD}% | Interval: {SCAN_INTERVAL}s")
        try:
            while True:
                await self.scan()
                await asyncio.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopping bot...")
        finally:
            await self.session.close()

app = Flask('')
@app.route('/')
def home(): return "Bot is running!"
def run_web(): app.run(host='0.0.0.0', port=10000)
threading.Thread(target=run_web).start()

if __name__ == "__main__":
    print("BOT START HUA HAI BHAI")
    asyncio.run(AlertBot().run())
