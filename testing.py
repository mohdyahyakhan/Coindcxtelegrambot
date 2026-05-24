import requests
import time
import os
from flask import Flask
import threading

app = Flask(__name__)

PUMP_PERCENT = 40
WATCHLIST = {}
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

def bot1_scan_bybit_futures():
    print("Bot1 Bybit Futures thread started", flush=True)
    while True:
        try:
            # Bybit USDT Perpetual - Futures only
            url = "https://api.bybit.com/v5/market/tickers"
            params = {'category': 'linear'} # linear = USDT perpetual futures
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, params=params, headers=headers, timeout=20)
            data = response.json()

            if data['retCode']!= 0:
                print(f"Bybit API Error: {data}", flush=True)
                time.sleep(60)
                continue

            tickers = data['result']['list']
            print(f"Bybit Futures pairs found: {len(tickers)}", flush=True)

            for ticker in tickers:
                symbol = ticker['symbol'] # BSBUSDT format
                if not symbol.endswith('USDT'):
                    continue

                if 'BSB' in symbol:
                    print(f"FOUND BSB: {symbol}", flush=True)

                # Bybit me 24h change = price24hPcnt * 100
                change_24h = float(ticker['price24hPcnt']) * 100

                if 'BSB' in symbol:
                    print(f"BSB 24h data: {change_24h:.2f}%", flush=True)

                if abs(change_24h) >= PUMP_PERCENT:
                    if symbol not in WATCHLIST:
                        WATCHLIST[symbol] = time.time()
                        cdcx_name = symbol.replace('USDT', '-USDT')
                        msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                              f"<b>Coin:</b> {cdcx_name}\n" \
                              f"<b>24h Change:</b> {change_24h:.2f}%\n" \
                              f"<b>Source:</b> Bybit Futures\n\n" \
                              f"Added to watchlist for 2 days."
                        send_telegram(msg)
                        print(f"Bot1 Alert: {cdcx_name} {change_24h:.2f}%", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300) # 5 min wait - Bybit ka rate limit 120 req/min hai

@app.route('/')
def home():
    return "Bot is running"

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
