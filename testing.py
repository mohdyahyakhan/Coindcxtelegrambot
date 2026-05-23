import asyncio
import aiohttp
from datetime import datetime, timedelta
import pytz
from flask import Flask
import threading
import os
import pandas as pd # Added - kyunki code me use ho raha hai

# ============ CONFIG ============
TELEGRAM_BOT_TOKEN = "8906533334:AAHI1LT_kPuGex0ved3juNjjgfjuEVFONy0" # Yaha apna token daal
TELEGRAM_CHAT_ID = "-5212565182" # Yaha chat ID daal
ALERT_THRESHOLD = 40.0 # 40%+ pump
ALERT_COOLDOWN = 21600 # 6 hours in seconds
WATCHLIST_DAYS = 2
SCAN_INTERVAL = 300 # 5 minutes

# Supertrend params
ST_PERIOD = 10
ST_MULTIPLIER = 3
EMA_PERIOD = 300

# ============ FLASK APP FOR RENDER ============
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running!"

# ============ BOT CLASS ============
class PumpBot:
    def __init__(self):
        self.last_alerted = {}
        self.watchlist = {}
        self.ist = pytz.timezone('Asia/Kolkata')

    async def send_telegram(self, msg):
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": msg,
            "parse_mode": "Markdown"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data) as resp:
                return await resp.json()

    async def get_tickers(self):
        url = "https://api.coindcx.com/exchange/ticker"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                return await resp.json()

    async def get_candles(self, symbol):
        candles_url = f"https://api.coindcx.com/market_data/candles?pair={symbol}&interval=5m&limit=350"
        async with aiohttp.ClientSession() as session:
            async with session.get(url=candles_url) as resp:
                data = await resp.json()
                if isinstance(data, list) and len(data) > 0:
                    df = pd.DataFrame(data, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
                    df['time'] = pd.to_datetime(df['time'], unit='ms')
                    df = df.astype({'open': float, 'high': float, 'low': float, 'close': float, 'volume': float})
                    return df
                return pd.DataFrame()

    def calculate_supertrend(self, df):
        hl2 = (df['high'] + df['low']) / 2
        atr = df['high'].rolling(ST_PERIOD).max() - df['low'].rolling(ST_PERIOD).min()
        atr = atr.rolling(ST_PERIOD).mean()

        upperband = hl2 + (ST_MULTIPLIER * atr)
        lowerband = hl2 - (ST_MULTIPLIER * atr)

        supertrend = [True] * len(df)
        final_upperband = [0] * len(df)
        final_lowerband = [0] * len(df)

        for i in range(1, len(df)):
            if df['close'][i] > final_upperband[i-1]:
                supertrend[i] = True
            elif df['close'][i] < final_lowerband[i-1]:
                supertrend[i] = False
            else:
                supertrend[i] = supertrend[i-1]

                if supertrend[i] and lowerband[i] < final_lowerband[i-1]:
                    final_lowerband[i] = final_lowerband[i-1]
                else:
                    final_lowerband[i] = lowerband[i]

                if not supertrend[i] and upperband[i] > final_upperband[i-1]:
                    final_upperband[i] = final_upperband[i-1]
                else:
                    final_upperband[i] = upperband[i]

        df['supertrend'] = [final_lowerband[i] if supertrend[i] else final_upperband[i] for i in range(len(df))]
        df['st_direction'] = supertrend
        return df

    def calculate_ema(self, df):
        df['ema300'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
        return df

    async def check_confirmation(self, symbol): # Checks 5m Supertrend
        df = await self.get_candles(symbol)
        if len(df) < EMA_PERIOD + 50:
            return False, None

        df = self.calculate_supertrend(df)
        df = self.calculate_ema(df)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Condition 1: ST crossed above EMA300
        st_crossed = prev['supertrend'] < prev['ema300'] and last['supertrend'] > last['ema300']

        # Condition 2: Price dipped below ST after cross
        price_below_st = last['close'] < last['supertrend'] and last['st_direction']

        if st_crossed:
            self.watchlist[symbol]['crossed'] = True

        if self.watchlist[symbol].get('crossed') and price_below_st:
            return True, last['close']

        return False, None

    async def scan(self):
        print(f"\n[{datetime.now(self.ist).strftime('%Y-%m-%d %H:%M:%S')}] Scanning 200+ markets")
        tickers = await self.get_tickers()
        now = datetime.now().timestamp()
        ts = datetime.now(self.ist).strftime('%Y-%m-%d %H:%M:%S IST')

        # Bot 1: 40%+ Pump Alert
        for symbol, t in tickers.items():
            if not symbol.startswith('B-'): continue

            coin = symbol.replace('B-', '').replace('_USDT', '')
            change_24h = float(t.get('change_24_hour', t.get('pc', 0)))
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
                    self.watchlist[symbol] = {'added': now, 'alerted': False, 'crossed': False}
                    print(f"PUMP Alert sent for {coin} + Added to watchlist")

        # Bot 2: ST+EMA300 Confirmation
        expired = []
        for symbol, data in self.watchlist.items():
            if now - data['added'] > WATCHLIST_DAYS * 86400:
                expired.append(symbol)
                continue

            if not data['alerted']:
                confirmed, entry = await self.check_confirmation(symbol)
                if confirmed:
                    coin = symbol.replace('B-', '').replace('_USDT', '')
                    msg = (
                        f"🎯 *CONFIRMATION ENTRY ALERT*\n"
                        f"Coin: `{coin}`\n"
                        f"Signal: *Supertrend CROSSED ABOVE EMA300 + Price dipped below ST*\n"
                        f"Entry Zone: Near `${entry:,.4f}`\n"
                        f"Time: `{ts}`"
                    )
                    await self.send_telegram(msg)
                    self.watchlist[symbol]['alerted'] = True
                    print(f"CONFIRMATION Alert sent for {coin}")

        for symbol in expired:
            del self.watchlist[symbol]
            print(f"Removed {symbol} from watchlist - expired")

    async def run(self):
        await self.send_telegram("✅ *Bot 1: 40%+ Pump Alert - ACTIVE*\n✅ *Bot 2: ST+EMA300 Entry - ACTIVE | Watchlist 2 Days*\n\n_Scanning CoinDCX Futures every 5 min..._")
        while True:
            try:
                await self.scan()
            except Exception as e:
                print(f"Error in scan: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

def run_bot():
    bot = PumpBot()
    asyncio.run(bot.run())

if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True # Added: Flask band ho to bot bhi band ho jaye
    bot_thread.start()
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
