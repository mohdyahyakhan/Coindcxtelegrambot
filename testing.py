import asyncio
import aiohttp
import pandas as pd
import numpy as np
import os
import time
from datetime import datetime, timezone

# =============== EDIT THESE CREDENTIALS ===============
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")
# =====================================================

API_TICKERS = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"
API_BINANCE_CANDLES = "https://api.binance.com/api/v3/klines"
ALERT_THRESHOLD = 40.0
SCAN_INTERVAL = 300
ALERT_COOLDOWN = 21600

def calculate_supertrend(df, period=10, multiplier=3):
    hl2 = (df['high'] + df['low']) / 2
    atr = df['high'].rolling(period).max() - df['low'].rolling(period).min()
    atr = atr.rolling(period).mean()
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)

    supertrend = [np.nan] * len(df)
    direction = [True] * len(df)

    for i in range(1, len(df)):
        if df['close'].iloc[i] > upperband.iloc[i-1]:
            direction[i] = True
        elif df['close'].iloc[i] < lowerband.iloc[i-1]:
            direction[i] = False
        else:
            direction[i] = direction[i-1]
            if direction[i] and lowerband.iloc[i] < lowerband.iloc[i-1]:
                lowerband.iloc[i] = lowerband.iloc[i-1]
            if not direction[i] and upperband.iloc[i] > upperband.iloc[i-1]:
                upperband.iloc[i] = upperband.iloc[i-1]
        supertrend[i] = lowerband.iloc[i] if direction[i] else upperband.iloc[i]

    df['supertrend'] = supertrend
    return df

class AlertBot:
    def __init__(self):
        self.session = None
        self.last_alerted = {}

    async def send_telegram(self, text: str):
        if "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
            print("TELEGRAM NOT SETUP - Message:", text)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
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

    async def fetch_binance_candles(self, symbol):
        coin = symbol.replace('B-', '').replace('_USDT', '') + 'USDT'
        params = {
            "symbol": coin,
            "interval": "5m",
            "limit": 350
        }
        try:
            async with self.session.get(API_BINANCE_CANDLES, params=params, timeout=15) as r:
                if r.status!= 200:
                    print(f"Binance API Error {r.status} for {coin}")
                    return []
                data = await r.json()
                return [[i[0], i[1], i[2], i[3], i[4], i[5]] for i in data]
        except Exception as e:
            print(f"Binance fetch error {coin}: {e}")
            return []

    async def check_dump_signal(self, symbol):
        try:
            ohlcv = await self.fetch_binance_candles(symbol)
            if len(ohlcv) < 300:
                print(f"Not enough Binance candles for {symbol}: {len(ohlcv)}")
                return False, 0, 0, 0

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = df.astype(float)
            df['ema300'] = df['close'].ewm(span=300, adjust=False).mean()
            df = calculate_supertrend(df, 10, 3)

            last = df.iloc[-1]
            prev = df.iloc[-2]

            st_cross_below = prev['supertrend'] > prev['ema300'] and last['supertrend'] < last['ema300']
            price_below_st = last['close'] < last['supertrend']

            return st_cross_below and price_below_st, last['close'], last['supertrend'], last['ema300']
        except Exception as e:
            print(f"TA Error {symbol}: {e}")
            return False, 0, 0, 0

    async def scan(self):
        try:
            tickers = await self.fetch_perps()
            now = time.time()
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            print(f"[{ts}] Scanning {len(tickers)} markets...")

            if len(tickers) == 0:
                print("WARNING: Still 0 pairs found")
                return

            pumped = []
            for symbol, t in tickers.items():
                if not symbol.startswith('B-'): continue
                change_24h = float(t.get('pc', 0))
                if change_24h >= ALERT_THRESHOLD:
                    pumped.append(symbol)

            print(f"[{ts}] Found {len(pumped)} coins with 40%+ pump")

            for symbol in pumped:
                coin = symbol.replace('B-', '').replace('_USDT', '')
                last_time = self.last_alerted.get(symbol, 0)
                if now - last_time < ALERT_COOLDOWN: continue

                signal, price, st, ema = await self.check_dump_signal(symbol)

                print(f"Checking {coin}: Price={price:.4f}, ST={st:.4f}, EMA300={ema:.4f}, Signal={signal}")

                if signal:
                    msg = (
                        f"📉 <b>POST-PUMP DUMP SIGNAL</b>\n"
                        f"Coin: <code>{coin}</code>\n"
                        f"Source: CoinDCX Pump + Binance TA\n"
                        f"Condition: <b>40% Pump + ST(10,3) &lt; EMA300 + Price &lt; ST</b>\n"
                        f"Price: <b>${price:,.4f}</b>\n"
                        f"Supertrend: <b>${st:,.4f}</b>\n"
                        f"EMA 300: <b>${ema:,.4f}</b>\n"
                        f"Time: <code>{ts}</code>"
                    )
                    await self.send_telegram(msg)
                    self.last_alerted[symbol] = now
                    print(f"Dump alert sent for {coin}")

                await asyncio.sleep(1)

        except Exception as e:
            print(f"Scan error: {e}")
            await self.send_telegram(f"⚠️ Bot Error: {str(e)[:200]}")

    async def run(self):
        self.session = aiohttp.ClientSession()
        await self.send_telegram("✅ <b>Bot Started on Telebothost!</b>\nMonitoring CoinDCX pumps + Binance TA. Will alert on dump signals.")
        print("="*60)
        print("CoinDCX Post-Pump Dump Monitor")
        print("="*60)
        print(f"Logic: 40% Pump on CoinDCX + ST<EMA300 on Binance 5m")
        print(f"Threshold: +{ALERT_THRESHOLD}% | Interval: {SCAN_INTERVAL}s")
        print("✅ Monitor started!")
        try:
            while True:
                await self.scan()
                await asyncio.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopping bot...")
        finally:
            await self.session.close()

if __name__ == "__main__":
    print("Bot starting... All systems GO!")
    asyncio.run(AlertBot().run())
