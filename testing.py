import requests
import time
import os
import pandas as pd
import ta
from threading import Thread
from flask import Flask

# ===== CONFIG =====
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
PUMP_PERCENT = 40
TIMEFRAME = '5m' # Bot 2 ke liye
WATCHLIST = {} # { "BSB-USDT": timestamp }
WATCHLIST_DAYS = 2

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

# ===== BOT 1: 24H 40%+ PUMP SCANNER =====
def bot1_scan_24h_pump():
    while True:
        try:
            url = "https://api.coindcx.com/exchange/v1/markets_details"
            res = requests.get(url, timeout=15).json()

            print(f"Total markets found: {len(res)}") # Debug line

            bsb_found = False
            for market in res:
                pair = market.get('pair', '')
                if 'BSB' in pair: # BSB jisme bhi hai wo print kar
                    print(f"DEBUG BSB: {pair} | market: {market.get('market')} | 24h: {market.get('change_24_hour')}%")
                    bsb_found = True

                if market.get('market')!= 'futures':
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
                        print(f"Bot1 Alert: {symbol} {change_24h}%")

            if not bsb_found:
                print("BSB pair API me mila hi nahi")

        except Exception as e:
            print(f"Bot1 Error: {e}")

        time.sleep(300)
        if __name__ == '__main__':
    threading.Thread(target=bot1_scan_24h_pump, daemon=True).start()
    threading.Thread(target=bot2_check_entry, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
        
# ===== BOT 2: ST 10/3 vs EMA300 CROSS =====
def get_candles(symbol, interval='5m', limit=400):
    try:
        url = f"https://public.coindcx.com/market_data/candles?pair={symbol}&interval={interval}&limit={limit}"
        res = requests.get(url, timeout=15).json()
        df = pd.DataFrame(res)
        df = df.rename(columns={'open':'open','high':'high','low':'low','close':'close','volume':'volume','time':'time'})
        df[['open','high','low','close','volume']] = df[['open','high','low','close','volume']].astype(float)
        return df
    except:
        return None

def bot2_check_entry():
    while True:
        try:
            current_time = time.time()
            coins_to_remove = []

            for symbol, added_time in WATCHLIST.items():
                # 2 din se purana ho gaya to hata do
                if current_time - added_time > WATCHLIST_DAYS * 86400:
                    coins_to_remove.append(symbol)
                    continue

                df = get_candles(symbol, TIMEFRAME)
                if df is None or len(df) < 301:
                    continue

                # Indicators
                df['ema300'] = ta.ema(df['close'], length=300)
                df['supertrend'] = ta.trend.SuperTrendIndicator(df['high'], df['low'], df['close'], window=10, multiplier=3).super_trend()

                # Last 2 candles me cross check karo
                if len(df) < 2:
                    continue

                prev_st = df['supertrend'].iloc[-2]
                prev_ema = df['ema300'].iloc[-2]
                curr_st = df['supertrend'].iloc[-1]
                curr_ema = df['ema300'].iloc[-1]

                # Neeche se cross: pehle ST < EMA tha, ab ST > EMA ho gaya
                if prev_st < prev_ema and curr_st > curr_ema:
                    msg = f"✅ <b>BOT 2: ENTRY CONFIRMED</b> ✅\n\n" \
                          f"<b>Coin:</b> {symbol}\n" \
                          f"<b>Signal:</b> Supertrend 10,3 crossed above EMA 300\n" \
                          f"<b>TF:</b> 5 Minutes\n" \
                          f"<b>ST:</b> {curr_st:.4f}\n" \
                          f"<b>EMA300:</b> {curr_ema:.4f}\n\n" \
                          f"24h pump ke baad entry mil gayi."
                    send_telegram(msg)
                    coins_to_remove.append(symbol) # Alert ke baad hata do
                    print(f"Bot2 Entry: {symbol}")

            for coin in coins_to_remove:
                del WATCHLIST[coin]

        except Exception as e:
            print(f"Bot2 Error: {e}")

        time.sleep(60) # Har 1 min me entry check

# ===== START BOTS =====
def run_bots():
    time.sleep(5)
    send_telegram("✅ <b>Bot Started</b>\n\nBot 1: 24h 40%+ Pump Scanner - ACTIVE\nBot 2: ST 10/3 vs EMA 300 Entry - ACTIVE\nWatchlist: 2 Days\n\nScanning CoinDCX Futures...")
    Thread(target=bot1_scan_24h_pump).start()
    Thread(target=bot2_check_entry).start()

if __name__ == '__main__':
    threading.Thread(target=bot1_scan_24h_pump, daemon=True).start()
    threading.Thread(target=bot2_check_entry, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
