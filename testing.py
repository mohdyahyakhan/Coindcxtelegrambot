import threading
import requests
import time
import os
import json
import asyncio
from flask import Flask
import pandas as pd
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running", 200

PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300

GIST_ID = "5ef25a569ac5dcb8b1a7425aab22cced"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}"

WATCHLIST = {}
PAPER_TRADES = {}
TICKER_HISTORY = {}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
total_pnl_lifetime = 0.0
last_ticker_save = 0

def gist_get(filename):
    try:
        res = requests.get(GIST_URL, headers=GIST_HEADERS, timeout=10).json()
        content = res['files'][filename]['content']
        return json.loads(content)
    except: return None

def gist_save(filename, data):
    try:
        payload = {"files": {filename: {"content": json.dumps(data, indent=2)}}}
        requests.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=10)
    except: pass

def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    WATCHLIST = data if isinstance(data, dict) else {}

def save_watchlist(): gist_save('watchlist.json', WATCHLIST)

def load_paper_trades():
    global PAPER_TRADES
    data = gist_get('paper_trades.json')
    if isinstance(data, dict) and 'trades' in data:
        PAPER_TRADES = data['trades']
    else:
        PAPER_TRADES = {}
        gist_save('paper_trades.json', {'trades': {}})
    print(f"Loaded {len(PAPER_TRADES)} paper trades", flush=True)

def save_paper_trades(): gist_save('paper_trades.json', {'trades': PAPER_TRADES})

def load_total_pnl():
    global total_pnl_lifetime
    data = gist_get('lifetime_pnl.json')
    total_pnl_lifetime = data.get('total_pnl', 0.0) if isinstance(data, dict) else 0.0

def save_total_pnl(value):
    global total_pnl_lifetime
    total_pnl_lifetime = value
    gist_save('lifetime_pnl.json', {'total_pnl': total_pnl_lifetime})

def load_ticker_history():
    global TICKER_HISTORY
    data = gist_get('ticker_history.json')
    TICKER_HISTORY = data if isinstance(data, dict) else {}

def save_ticker_history():
    global last_ticker_save
    data_to_save = {k: v for k, v in TICKER_HISTORY.items() if k in WATCHLIST}
    gist_save('ticker_history.json', data_to_save)
    last_ticker_save = time.time()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=10)

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin = context.args[0].upper() if context.args else update.message.text.replace("ADD ", "").strip().upper()
    if not coin: return await update.message.reply_text("Use: /add BTCUSDT")
    if not coin.endswith("USDT"): coin += "USDT"
    global WATCHLIST
    if coin not in WATCHLIST:
        WATCHLIST[coin] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset'}
        save_watchlist()
        await update.message.reply_text(f"✅ {coin} added")
    else:
        await update.message.reply_text(f"⚠️ {coin} already in watchlist")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin = context.args[0].upper() if context.args else "BTCUSDT"
    if not coin.endswith("USDT"): coin += "USDT"
    WATCHLIST.pop(coin, None)
    save_watchlist()
    await update.message.reply_text(f"❌ {coin} removed")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = "\n".join([f"• {c}" for c in WATCHLIST.keys()]) if WATCHLIST else "Khali hai"
    await update.message.reply_text(f"<b>WATCHLIST</b>\n\n{coins}", parse_mode="HTML")

# YAHAN NAYA BOT1 PASTE KIYA
async def bot1_scan():
    global last_ticker_save
    load_watchlist(); load_ticker_history()
    print("Bot1 started with FUTURES ticker", flush=True)
    first_run = True
    while True:
        try:
            res = requests.get(
                "https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments",
                timeout=20
            ).json()

            if first_run:
                print(f"Bot1 DEBUG sample: {str(res)[:800]}", flush=True)
                first_run = False

            if isinstance(res, list):
                for t in res:
                    if isinstance(t, dict) and 'USDT' in str(t):
                        symbol = t.get('pair') or t.get('symbol') or t.get('instrument') or t.get('s')
                        price = t.get('last_price') or t.get('mark_price') or t.get('ls') or t.get('l') or t.get('mp')
                        if symbol and price:
                            symbol = str(symbol).replace('B-','').replace('_','').upper()
                            TICKER_HISTORY.setdefault(symbol,[]).append(float(price))
                            if len(TICKER_HISTORY[symbol])>1000: TICKER_HISTORY[symbol].pop(0)
            else:
                print(f"Bot1: API ne list nahi bheja: {str(res)[:300]}", flush=True)

            if time.time()-last_ticker_save>300: save_ticker_history()
        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(300)

async def bot2_scan():
    print("Bot2 started", flush=True)
    while True:
        try:
            for symbol in list(WATCHLIST.keys()):
                pass # yaha tera supertrend logic rahega
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(30)

def main():
    load_watchlist(); load_paper_trades(); load_total_pnl(); load_ticker_history()
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("add", add_command))
    telegram_app.add_handler(CommandHandler("remove", remove_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(telegram_app.bot.delete_webhook(drop_pending_updates=True))
    loop.run_until_complete(telegram_app.bot.initialize())

    port = int(os.environ.get("PORT", 10000))
    flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, threaded=True), daemon=True)
    flask_thread.start()
    print(f"Flask started on port {port}", flush=True)
    time.sleep(3)

    loop.create_task(bot1_scan())
    loop.create_task(bot2_scan())

    loop.run_until_complete(telegram_app.run_polling(drop_pending_updates=True))

if __name__ == '__main__': main()