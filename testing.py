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
            url = "https://api.coindcx.com/exchange/v1/markets_details"
            res = requests.get(url, timeout=15).json()

            print(f"Total markets found: {len(res)}", flush=True)

            bsb_found = False
            for market in res:
                pair = market.get('pair', '')
                market_type = market.get('market', '')
                
                # BSB ka exact data print karo, bina filter ke
                if 'BSB' in pair.upper():
                    print(f"FOUND BSB: pair='{pair}' | market='{market_type}' | 24h={market.get('change_24_hour')}%", flush=True)
                    bsb_found = True

                # Ab filter lagao
                if market_type!= 'futures':
                    continue
                if not pair.endswith('USDT'):
                    continue

                symbol = market['pair']
                change_24h = float(market.get('change_24_hour', 0))

                if abs(change_24h) >= PUMP_PERCENT:
                    if symbol not in WATCHLIST:
                        WATCHLIST[symbol] = time.time()
                        msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                              f"<b>Coin:</b> {symbol}\n" \
                              f"<b>24h Change:</b> {change_24h}%\n\n" \
                              f"Added to watchlist for 2 days.\nScanning for ST+EMA300 entry..."
                        send_telegram(msg)
                        print(f"Bot1 Alert: {symbol} {change_24h}%", flush=True)

            if not bsb_found:
                print("BSB pair API me mila hi nahi", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300)
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
