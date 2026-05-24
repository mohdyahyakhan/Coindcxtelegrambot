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
        print("Telegram token/chat_id missing in env", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def bot1_scan_24h_pump():
    print("Bot1 thread started", flush=True)
    while True:
        try:
            url1 = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
            instruments = requests.get(url1, timeout=15).json()
            print(f"Futures pairs found: {len(instruments)}", flush=True)

            url2 = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/tickers"
            ticker_data = requests.get(url2, timeout=15).json()
            
            # DEBUG: Ticker me kitni keys hain aur kaunsi hain
            if isinstance(ticker_data, dict):
                ticker_keys = list(ticker_data.keys())
                print(f"Tickers received: {len(ticker_keys)}", flush=True)
                print(f"Sample keys: {ticker_keys[:5]}", flush=True) # Pehli 5 keys dikhao
            else:
                print(f"Ticker API error: {ticker_data}", flush=True)
                time.sleep(300)
                continue

            for pair in instruments:
                if 'BSB' in pair.upper():
                    print(f"FOUND BSB in instruments: {pair}", flush=True)

                if not pair.endswith('_USDT'):
                    continue

                ticker = ticker_data.get(pair)
                if not ticker:
                    if 'BSB' in pair.upper():
                        print(f"BSB key not found in ticker_data", flush=True)
                    continue

                change_24h = float(ticker.get('P', 0))

                if 'BSB' in pair.upper():
                    print(f"BSB 24h data: {change_24h}%", flush=True)

                clean_pair = pair.replace('B-', '').replace('_', '-')

                if abs(change_24h) >= PUMP_PERCENT:
                    if clean_pair not in WATCHLIST:
                        WATCHLIST = time.time()
                        msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                              f"<b>Coin:</b> {clean_pair}\n" \
                              f"<b>24h Change:</b> {change_24h}%\n\n" \
                              f"Added to watchlist for 2 days."
                        send_telegram(msg)
                        print(f"Bot1 Alert: {clean_pair} {change_24h}%", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300)
@app.route('/')
def home():
    return "Bot is running"

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    threading.Thread(target=bot1_scan_24h_pump, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
