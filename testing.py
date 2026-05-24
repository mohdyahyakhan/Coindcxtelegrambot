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
            # Step 1: Sab active futures pairs lo
            url1 = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
            instruments = requests.get(url1, timeout=15).json()
            print(f"Futures pairs found: {len(instruments)}", flush=True)

            # Step 2: /exchange/ticker se sabka data lo - ye wala kaam karta hai
            url2 = "https://api.coindcx.com/exchange/ticker"
            tickers = requests.get(url2, timeout=15).json()
            print(f"Tickers received: {len(tickers)}", flush=True)
            
            # List ko dict me convert karo fast lookup ke liye
            ticker_dict = {item['market']: item for item in tickers if 'market' in item}

            for pair in instruments:
                if 'BSB' in pair.upper():
                    print(f"FOUND BSB in instruments: {pair}", flush=True)

                # B-BSB_USDT ko BSBUSDT me convert karo kyunki /exchange/ticker me aisa naam hai
                ticker_key = pair.replace('B-', '').replace('_', '')
                
                ticker = ticker_dict.get(ticker_key)
                if not ticker:
                    if 'BSB' in pair.upper():
                        print(f"BSB key '{ticker_key}' not found in ticker_dict", flush=True)
                    continue

                # /exchange/ticker me 'change_24_hour' field hoti hai
                change_24h = float(ticker.get('change_24_hour', 0))

                if 'BSB' in pair.upper():
                    print(f"BSB 24h data: {change_24h}%", flush=True)

                # Display ke liye clean naam
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
