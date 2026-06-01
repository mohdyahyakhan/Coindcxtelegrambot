import os
import time
import requests
import threading
from collections import defaultdict
from datetime import datetime
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running", 200

def run_flask():
    app.run(host="0.0.0.0", port=10000)

threading.Thread(target=run_flask, daemon=True).start()

# ====== CONFIG ======
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
PUMP_PERCENT_24H = 10 # Testing ke liye 10%. Baad mein 40 kar dena
MAX_ALERTS_PER_COIN = 3
BYBIT_CYCLE_SEC = 300
CDCX_CYCLE_SEC = 300
BOT2_CYCLE_SEC = 30

alerted_symbols = defaultdict(int)

COINDX_FUTURES = {
    'STGUSDT', 'WLDUSDT', 'ACUUSDT', 'AIGENSYNUSDT', 'BANUSDT', 'BILLUSDT',
    'EPICUSDT', 'FFUSDT', 'FHEUSDT', 'HOMEUSDT', 'HUSDT', 'ICNTUSDT',
    'INITUSDT', 'MBOXUSDT', 'MERLUSDT', 'NOMUSDT', 'PORTALUSDT', 'RONINUSDT',
    'VICUSDT'
}

def send_telegram_alert(text):
    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    try:
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': text}, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)

def process_pump_alert(symbol, change_24h, price, source, alerted_dict):
    if alerted_dict[symbol] >= MAX_ALERTS_PER_COIN:
        print(f"{symbol} Limit {MAX_ALERTS_PER_COIN}/{MAX_ALERTS_PER_COIN} reach — skip", flush=True)
        return False

    alerted_dict[symbol] += 1
    msg = f"🔥 BOT1: {symbol} PUMP ALERT #{alerted_dict[symbol]}/{MAX_ALERTS_PER_COIN}\n"
    msg += f"Exchange: {source}\n"
    msg += f"24h Change: {change_24h:.2f}%\n"
    msg += f"Price: ${price}\n"
    msg += f"Time: {datetime.now().strftime('%H:%M:%S')}"
    send_telegram_alert(msg)
    print(f"Bot1 Alert [{source}]: {symbol} {change_24h:.2f}%", flush=True)
    return True

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
        return r.json()
    except Exception as e:
        print(f"CoinDCX API error: {e}", flush=True)
        return []

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
                # Fix: handle both F-VICUSDT and F-VIC_USDT
                base = market.replace('F-', '').replace('_USDT', '').replace('USDT', '')
                symbol = f"{base}USDT"
                cdcx_map[symbol] = item

            print(f"CoinDCX Map has VICUSDT: {'VICUSDT' in cdcx_map}", flush=True)

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

                    # VIC Debug
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

def calculate_st_ema(prices, st_len=10, st_mult=3, ema_len=300):
    if len(prices) < ema_len:
        return None, None

    # Simple ST calculation
    atr = sum(abs(prices[i] - prices[i-1]) for i in range(1, min(st_len+1, len(prices)))) / st_len
    st = prices[-1] - st_mult * atr

    # Simple EMA calculation
    k = 2 / (ema_len + 1)
    ema = prices[0]
    for p in prices[1:]:
        ema = p * k + ema * (1 - k)

    return st, ema

def bot2_check_signals():
    while True:
        try:
            watchlist = list(alerted_symbols.keys())
            if not watchlist:
                time.sleep(BOT2_CYCLE_SEC)
                continue

            print(f"Bot2: ===== NEW CYCLE - {len(watchlist)} coins =====", flush=True)

            for symbol in watchlist:
                if alerted_symbols[symbol] >= MAX_ALERTS_PER_COIN:
                    continue

                # Get 5min candles from Bybit
                url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={symbol}&interval=5&limit=300"
                try:
                    r = requests.get(url, timeout=10)
                    candles = r.json().get('result', {}).get('list', [])
                    if len(candles) < 300:
                        continue

                    closes = [float(c[4]) for c in candles[::-1]]
                    price = closes[-1]
                    st, ema300 = calculate_st_ema(closes)

                    if st is None:
                        continue

                    signal = price < st < ema300
                    print(f"Bot2: [{symbol}] Price={price:.6f} | ST={st:.6f} | EMA300={ema300:.6f} | SIGNAL={signal}", flush=True)

                    if signal:
                        alerted_symbols[symbol] += 1
                        msg = f"🔻 BOT 2: SHORT SIGNAL #{alerted_symbols[symbol]}/3 🔻\n\n"
                        msg += f"Coin: {symbol}\n"
                        msg += f"Condition: Price < ST < EMA300\n"
                        msg += f"Signal: #{alerted_symbols[symbol]}/3\n"
                        msg += f"Timeframe: 5min\n"
                        msg += f"Price: ${price:.6f}\n"
                        msg += f"ST(10,3): ${st:.6f}\n"
                        msg += f"EMA300: ${ema300:.6f}\n\n"
                        msg += f"📊 Price < Supertrend < EMA300\n"
                        msg += f"🎯 CoinDCX Futures pe SHORT entry zone.\n"
                        msg += f"🛑 SL: ST ke upar → ${st:.6f}"
                        send_telegram_alert(msg)
                        print(f"Bot2: [{symbol}] ✅ SHORT ALERT #{alerted_symbols[symbol]}!", flush=True)

                except Exception as e:
                    print(f"Bot2 Error {symbol}: {e}", flush=True)

            print(f"Bot2: ===== CYCLE COMPLETE - {BOT2_CYCLE_SEC}s wait =====", flush=True)

        except Exception as e:
            print(f"Bot2 Main Error: {e}", flush=True)
        time.sleep(BOT2_CYCLE_SEC)

if __name__ == "__main__":
    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    threading.Thread(target=bot2_check_signals, daemon=True).start()

    while True:
        time.sleep(60)
