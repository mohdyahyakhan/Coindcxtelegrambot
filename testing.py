import requests
import time
import os
from flask import Flask
import threading

app = Flask(__name__)

PUMP_PERCENT = 40
WATCHLIST = {}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN") # ← Render env se lega
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID") # ← Render env se lega

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
            # CoinDCX Futures ka asli API
            url = "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments"
            res = requests.get(url, timeout=15).json()
            print(f"Futures pairs found: {len(res)}", flush=True)

            for market in res:
                pair = market.get('pair', '') # Yaha 'BSB-USDT' aayega
                change_24h = float(market.get('change_24_hour_percent', 0))

                # BSB ka debug - isse pata chalega mil raha ya nahi
                if 'BSB' in pair.upper():
                    print(f"FOUND BSB: {pair} | 24h: {change_24h}%", flush=True)

                # Sirf USDT pairs check karo
                if not pair.endswith('USDT'):
                    continue

                # 40% pump/dump check
                if abs(change_24h) >= PUMP_PERCENT:
                    if pair not in WATCHLIST:
                        WATCHLIST[pair] = time.time() # ← Ye line fix ki hai
                        msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                              f"<b>Coin:</b> {pair}\n" \
                              f"<b>24h Change:</b> {change_24h}%\n\n" \
                              f"Added to watchlist for 2 days."
                        send_telegram(msg)
                        print(f"Bot1 Alert: {pair} {change_24h}%", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300) # 5 min me check karega
def bot2_check_entry():
    print("Bot2 thread started", flush=True)
    while True:
        # Tera bot2 ka code yaha rahega
        time.sleep(60)

@app.route('/')
def home():
    return "Bot is running"

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    threading.Thread(target=bot1_scan_24h_pump, daemon=True).start()
    threading.Thread(target=bot2_check_entry, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
