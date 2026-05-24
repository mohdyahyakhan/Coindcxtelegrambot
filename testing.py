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
        print("Telegram token/chat_id missing", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def bot1_scan_binance_spot():
    print("Bot1 Binance Spot thread started", flush=True)
    while True:
        try:
            # Futures ki jagah Spot API - ye ban nahi hai
            url = "https://api.binance.com/api/v3/ticker/24hr"
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, timeout=15, headers=headers)
            tickers = response.json()

            if isinstance(tickers, dict) and 'code' in tickers:
                print(f"Binance Spot API Error: {tickers}", flush=True)
                time.sleep(60)
                continue

            print(f"Binance Spot pairs found: {len(tickers)}", flush=True)

            for ticker in tickers:
                symbol = ticker['symbol']
                if not symbol.endswith('USDT'):
                    continue

                if 'BSB' in symbol:
                    print(f"FOUND BSB: {symbol}", flush=True)

                change_24h = float(ticker['priceChangePercent'])

                if 'BSB' in symbol:
                    print(f"BSB 24h data: {change_24h}%", flush=True)

                if abs(change_24h) >= PUMP_PERCENT:
                    if symbol not in WATCHLIST:
                        WATCHLIST[symbol] = time.time()
                        cdcx_name = symbol.replace('USDT', '-USDT')
                        msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                              f"<b>Coin:</b> {cdcx_name}\n" \
                              f"<b>24h Change:</b> {change_24h}%\n" \
                              f"<b>Source:</b> Binance Spot\n\n" \
                              f"Added to watchlist for 2 days."
                        send_telegram(msg)
                        print(f"Bot1 Alert: {cdcx_name} {change_24h}%", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300)

@app.route('/')
def home():
    return "Bot is running"

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    threading.Thread(target=bot1_scan_binance_spot, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
