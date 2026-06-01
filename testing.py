import os
import time
import requests
import pandas as pd
import threading
from datetime import datetime
from flask import Flask
import ta

app = Flask(__name__)

# ========== CONFIG ==========
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
CHAT_ID = os.getenv("CHAT_ID", "YOUR_CHAT_ID_HERE")

PUMP_PERCENT_24H = 40
BYBIT_CYCLE_SEC = 60
EMA_PERIOD = 300

# CoinDCX Futures watchlist
COINDX_FUTURES = {'VICUSDT', 'STGUSDT', 'WLDUSDT'}

alerted_symbols = set()
bot2_active_symbols = set()

# ========== TELEGRAM ==========
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": CHAT_ID, "text": text}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

# ========== API HELPERS ==========
def get_bybit_futures_tickers():
    url = "https://api.bybit.com/v5/market/tickers?category=linear"
    try:
        r = requests.get(url, timeout=10)
        return r.json().get('result', {}).get('list', [])
    except Exception as e:
        print(f"Bybit API error: {e}", flush=True)
        return []

def get_coindcx_futures_tickers():
    url = "https://public.coindcx.com/exchange/trades/v1/derivatives/futures_data"
    try:
        r = requests.get(url, timeout=10)
        print(f"CoinDCX Status: {r.status_code}", flush=True)
        print(f"CoinDCX Raw Text: {r.text[:500]}", flush=True)  # First 500 chars
        data = r.json()
        if isinstance(data, dict):
            data = data.get('data', [])
        return data
    except Exception as e:
        print(f"CoinDCX API error: {e}", flush=True)
        return []

def get_bybit_klines(symbol, interval="1", limit=300):
    url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval={interval}&limit={limit}"
    try:
        r = requests.get(url, timeout=10)
        return r.json().get('result', {}).get('list', [])
    except Exception as e:
        print(f"Kline error {symbol}: {e}", flush=True)
        return []

# ========== INDICATORS ==========
def supertrend(df, period=10, multiplier=3):
    hl2 = (df['high'] + df['low']) / 2
    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=period).average_true_range()
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)
    st = [True] * len(df)
    for i in range(1, len(df)):
        if df['close'].iloc[i] > upperband.iloc[i-1]:
            st[i] = True
        elif df['close'].iloc[i] < lowerband.iloc[i-1]:
            st[i] = False
        else:
            st[i] = st[i-1]
            if st[i] and lowerband.iloc[i] < lowerband.iloc[i-1]:
                lowerband.iloc[i] = lowerband.iloc[i-1]
            if not st[i] and upperband.iloc[i] > upperband.iloc[i-1]:
                upperband.iloc[i] = upperband.iloc[i-1]
    return pd.Series([lowerband.iloc[i] if st[i] else upperband.iloc[i] for i in range(len(df))], index=df.index)

def calculate_ema(df, period=EMA_PERIOD):
    return ta.trend.EMAIndicator(df['close'], window=period).ema_indicator()

# ========== BOT1: PUMP SCANNER ==========
def process_pump_alert(symbol, change_24h, price, source, alerted_set):
    if symbol not in alerted_set:
        alerted_set.add(symbol)
        msg = f"Bot1 Alert [{source}]: {symbol} {change_24h:.2f}%"
        print(msg, flush=True)
        send_telegram(msg)
        return True
    return False

def bot1_scan_bybit_futures():
    while True:
        try:
            bybit_data = get_bybit_futures_tickers()
            pumped_bybit = 0
            bybit_symbols_found = set()

            for ticker in bybit_data:
                symbol = ticker.get('symbol', '')
                if not symbol.endswith('USDT'):
                    continue
                bybit_symbols_found.add(symbol)
                try:
                    change_24h = float(ticker.get('price24hPcnt', 0)) * 100
                    price = ticker.get('lastPrice', '0')
                    if change_24h >= PUMP_PERCENT_24H:
                        if process_pump_alert(symbol, change_24h, price, 'Bybit Futures', alerted_symbols):
                            pumped_bybit += 1
                except Exception:
                    continue

            cdcx_data = get_coindcx_futures_tickers()
            cdcx_map = {}
            for item in cdcx_data:
                market = item.get('market', '')
                if not market.startswith('F-'):
                    continue
                symbol = market.replace('F-', '')
                cdcx_map[symbol] = item

            print(f"CoinDCX Map has VICUSDT: {'VICUSDT' in cdcx_map}", flush=True)
            print(f"CoinDCX Map Keys Sample: {list(cdcx_map.keys())[:30]}", flush=True)

            coindcx_only = COINDX_FUTURES - bybit_symbols_found
            pumped_cdcx = 0

            print(f"Bot1 [CoinDCX]: {len(coindcx_only)} coins scan kar raha hoon...", flush=True)

            for symbol in coindcx_only:
                if symbol not in cdcx_map:
                    continue
                ticker = cdcx_map[symbol]
                try:
                    change_raw = ticker.get('change_24_hour') or ticker.get('change_24h') or 0
                    change_24h = float(change_raw)
                    price = ticker.get('last_price', '0')

                    if symbol == 'VICUSDT':
                        print(f"VIC DEBUG: change_raw={change_raw} float={change_24h} price={price}", flush=True)
                        print(f"VIC DEBUG: full ticker={ticker}", flush=True)

                    if change_24h >= PUMP_PERCENT_24H:
                        if process_pump_alert(symbol, change_24h, price, 'CoinDCX Futures', alerted_symbols):
                            pumped_cdcx += 1
                except Exception as e:
                    print(f"CoinDCX parse error {symbol}: {e} | data={ticker}", flush=True)
                    continue

            print(f"Bot1 [Bybit]: {len(bybit_data)} pairs checked | Pumped: {pumped_bybit}", flush=True)
            print(f"Bot1 [CoinDCX]: Scan complete | Pumped: {pumped_cdcx}", flush=True)
            print(f"Bot1: Total Watchlist: {len(alerted_symbols)} coins", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)
        time.sleep(BYBIT_CYCLE_SEC)

# ========== BOT2: ST + EMA SIGNAL ==========
def bot2_signal_checker():
    while True:
        try:
            if not alerted_symbols:
                time.sleep(30)
                continue

            current_symbols = list(alerted_symbols)
            print(f"Bot2: ===== NEW CYCLE - {len(current_symbols)} coins =====", flush=True)

            for symbol in current_symbols:
                try:
                    klines = get_bybit_klines(symbol, limit=EMA_PERIOD)
                    if len(klines) < EMA_PERIOD:
                        continue

                    df = pd.DataFrame(klines, columns=['time','open','high','low','close','volume','turnover'])
                    df = df.astype(float)

                    st = supertrend(df)
                    ema = calculate_ema(df)

                    price = df['close'].iloc[-1]
                    st_val = st.iloc[-1]
                    ema_val = ema.iloc[-1]

                    signal = price > st_val and price > ema_val

                    print(f"Bot2: [{symbol}] Price={price:.6f} | ST={st_val:.6f} | EMA{EMA_PERIOD}={ema_val:.6f} | SIGNAL={signal}", flush=True)

                    if signal and symbol not in bot2_active_symbols:
                        bot2_active_symbols.add(symbol)
                        msg = f"Bot2 SIGNAL: {symbol} | Price: {price:.6f} > ST & EMA{EMA_PERIOD}"
                        send_telegram(msg)

                except Exception as e:
                    print(f"Bot2 error {symbol}: {e}", flush=True)
                    continue

            print(f"Bot2: ===== CYCLE COMPLETE - 30s wait =====", flush=True)
            time.sleep(30)

        except Exception as e:
            print(f"Bot2 Error: {e}", flush=True)
            time.sleep(30)

# ========== FLASK ==========
@app.route('/')
def home():
    return "Bot Running", 200

# ========== START ==========
if __name__ == '__main__':
    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    threading.Thread(target=bot2_signal_checker, daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 10000)))
