import requests
import time
import os
from flask import Flask
import threading
import pandas as pd
import numpy as np

app = Flask(__name__)

PUMP_PERCENT = 15 # Bot1 trigger
WATCHLIST_DAYS = 2 # 2 din tak monitor
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300

WATCHLIST = {} # {'BSBUSDT': {'time': 123456, 'last_st': 'up'}}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def calculate_supertrend(df, period=10, multiplier=3):
    # ATR calculate
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].rolling(period).mean()

    # Basic bands
    df['upperband'] = (df['high'] + df['low']) / 2 + multiplier * df['atr']
    df['lowerband'] = (df['high'] + df['low']) / 2 - multiplier * df['atr']

    # Final bands
    df['final_upperband'] = df['upperband']
    df['final_lowerband'] = df['lowerband']

    for i in range(1, len(df)):
        if df['close'].iloc[i-1] <= df['final_upperband'].iloc[i-1]:
            df.loc[df.index[i], 'final_upperband'] = min(df['upperband'].iloc[i], df['final_upperband'].iloc[i-1])
        if df['close'].iloc[i-1] >= df['final_lowerband'].iloc[i-1]:
            df.loc[df.index[i], 'final_lowerband'] = max(df['lowerband'].iloc[i], df['final_lowerband'].iloc[i-1])

    # Supertrend
    df['supertrend'] = True # True = uptrend/green, False = downtrend/red
    for i in range(1, len(df)):
        if df['close'].iloc[i] <= df['final_lowerband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = False
        elif df['close'].iloc[i] >= df['final_upperband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = True
        else:
            df.loc[df.index[i], 'supertrend'] = df['supertrend'].iloc[i-1]

    df['ema300'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df

def get_klines(symbol, interval='5', limit=350):
    # Bybit kline API
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        'category': 'linear',
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res['retCode'] == 0:
            data = res['result']['list']
            df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume','turnover'])
            df = df.astype({'open': float, 'high': float, 'low': float, 'close': float})
            df = df.iloc[::-1].reset_index(drop=True) # Oldest first
            return df
    except Exception as e:
        print(f"Kline Error {symbol}: {e}", flush=True)
    return None

def bot1_scan_bybit_futures():
    print("Bot1 Bybit Futures thread started", flush=True)
    while True:
        try:
            url = "https://api.bybit.com/v5/market/tickers"
            params = {'category': 'linear'}
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, params=params, headers=headers, timeout=20)
            data = response.json()

            if data['retCode']!= 0:
                print(f"Bot1 API Error: {data}", flush=True)
                time.sleep(60)
                continue

            tickers = data['result']['list']
            print(f"Bot1: Bybit Futures pairs found: {len(tickers)}", flush=True)

            for ticker in tickers:
                symbol = ticker['symbol']
                if not symbol.endswith('USDT'):
                    continue

                change_24h = float(ticker['price24hPcnt']) * 100

                if symbol not in WATCHLIST and abs(change_24h) >= PUMP_PERCENT:
                    WATCHLIST[symbol] = {
                        'time': time.time(),
                        'last_st': None # Track supertrend state
                    }
                    cdcx_name = symbol.replace('USDT', '-USDT')
                    msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                          f"<b>Coin:</b> {cdcx_name}\n" \
                          f"<b>24h Change:</b> {change_24h:.2f}%\n" \
                          f"<b>Source:</b> Bybit Futures\n\n" \
                          f"Added to Bot2 watchlist for {WATCHLIST_DAYS} days."
                    send_telegram(msg)
                    print(f"Bot1 Alert: {cdcx_name} {change_24h:.2f}%", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300)

def bot2_supertrend_exit():
    print("Bot2 Supertrend thread started", flush=True)
    while True:
        try:
            if not WATCHLIST:
                time.sleep(60)
                continue

            print(f"Bot2: Monitoring {len(WATCHLIST)} coins for Supertrend", flush=True)
            to_remove = []

            for symbol, info in WATCHLIST.items():
                # 2 din expiry
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400:
                    to_remove.append(symbol)
                    print(f"Bot2: Removing {symbol} - 2 days over", flush=True)
                    continue

                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD:
                    continue

                df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)

                # Latest 2 candles check karo cross ke liye
                current_st = df['supertrend'].iloc[-1] # True=up, False=down
                prev_st = df['supertrend'].iloc[-2]
                current_ema = df['ema300'].iloc[-1]
                current_close = df['close'].iloc[-1]
                prev_close = df['close'].iloc[-2]

                cdcx_name = symbol.replace('USDT', '-USDT')

                # Condition: Supertrend down + Close below EMA300 + Naya cross
                # Supertrend False = red/downtrend
                if not current_st and current_close < current_ema:
                    # Cross confirm: pichli candle me ya to ST up tha, ya price EMA ke upar tha
                    if prev_st or prev_close > current_ema:
                        if info['last_st']!= 'down': # Duplicate alert avoid
                            msg = f"🔻 <b>BOT 2: SUPERTREND EXIT</b> 🔻\n\n" \
                                  f"<b>Coin:</b> {cdcx_name}\n" \
                                  f"<b>Reason:</b> Supertrend(10,3) crossed below EMA(300)\n" \
                                  f"<b>Timeframe:</b> 5min\n" \
                                  f"<b>Price:</b> ${current_close:.6f}\n" \
                                  f"<b>EMA300:</b> ${current_ema:.6f}\n\n" \
                                  f"Bot1 pump ke baad exit signal."
                            send_telegram(msg)
                            WATCHLIST[symbol]['last_st'] = 'down'
                            print(f"Bot2 EXIT Alert: {cdcx_name}", flush=True)
                else:
                    WATCHLIST[symbol]['last_st'] = 'up'

                time.sleep(1) # Rate limit bachane ke liye

            for symbol in to_remove:
                WATCHLIST.pop(symbol, None)

        except Exception as e:
            print(f"Bot2 Error: {e}", flush=True)

        time.sleep(120) # Har 2 min me check

@app.route('/')
def home():
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins"

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    threading.Thread(target=bot2_supertrend_exit, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
