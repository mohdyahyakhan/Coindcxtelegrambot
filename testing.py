import threading
import requests
import time
import os
import json
import asyncio
from flask import Flask, jsonify, request
import pandas as pd
import numpy as np
import math
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

app = Flask(__name__)

PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300

# GIST CONFIG
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
telegram_app = None
last_ticker_save = 0

# ===== NTFY FUNCTION =====
def send_ntfy_plain(msg):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic: return
    clean_msg = msg.replace('<b>', '').replace('</b>', '').replace('&lt;', '<').replace('&gt;', '>')
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=clean_msg.encode('utf-8'))
    except Exception as e:
        print(f"ntfy Error: {e}", flush=True)

# ===== GIST HELPER FUNCTIONS =====
def gist_get(filename):
    try:
        res = requests.get(GIST_URL, headers=GIST_HEADERS, timeout=10).json()
        content = res['files'][filename]['content']
        data = json.loads(content)
        return data
    except Exception as e:
        print(f"Gist GET error {filename}: {e}", flush=True)
        return None

def gist_save(filename, data):
    try:
        payload = {"files": {filename: {"content": json.dumps(data, indent=2)}}}
        requests.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=10)
    except Exception as e:
        print(f"Gist SAVE error {filename}: {e}", flush=True)

# ===== TELEGRAM COMMANDS =====
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin_text = ""
    if context.args:
        coin_text = context.args[0]
    else:
        coin_text = update.message.text.replace("ADD ", "").replace("add ", "").strip()

    if not coin_text:
        await update.message.reply_text("Use: /add COINNAME\nYa: ADD COINNAME\nEx: ADD BTCUSDT")
        return
    coin = coin_text.upper()
    if not coin.endswith("USDT"): coin = coin + "USDT"
    global WATCHLIST
    if coin not in WATCHLIST:
        WATCHLIST[coin] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'} # FIX: coin ke liye dict
        save_watchlist()
        await update.message.reply_text(f"✅ {coin} ko WATCHLIST me add kar diya")
    else:
        await update.message.reply_text(f"⚠️ {coin} pehle se WATCHLIST me hai")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin_text = ""
    if context.args:
        coin_text = context.args[0]
    else:
        coin_text = update.message.text.replace("REMOVE ", "").replace("remove ", "").strip()

    if not coin_text:
        await update.message.reply_text("Use: /remove COINNAME\nYa: REMOVE COINNAME\nEx: REMOVE BTCUSDT")
        return
    coin = coin_text.upper()
    if not coin.endswith("USDT"): coin = coin + "USDT"
    global WATCHLIST
    if coin in WATCHLIST:
        WATCHLIST.pop(coin)
        save_watchlist()
        await update.message.reply_text(f"❌ {coin} ko WATCHLIST se hata diya")
    else:
        await update.message.reply_text(f"⚠️ {coin} WATCHLIST me hai hi nahi")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WATCHLIST
    if not WATCHLIST:
        await update.message.reply_text("WATCHLIST khali hai\nCoin add karne ke liye: ADD BTCUSDT")
        return

    coins_list = []
    for coin, data in WATCHLIST.items():
        if isinstance(data, dict):
            coins_list.append(f"• {coin}")

    if not coins_list:
        await update.message.reply_text("WATCHLIST me data kharab hai. /REMOVE karke dubara ADD karo")
        return

    coins = "\n".join(coins_list)
    await update.message.reply_text(f"<b>WATCHLIST - {len(coins_list)} coins</b>\n\n{coins}", parse_mode="HTML")

# ===== WATCHLIST =====
def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    if data: WATCHLIST = data
    else: WATCHLIST = {}

def save_watchlist():
    try:
        gist_save('watchlist.json', WATCHLIST)
        print(f"Saved {len(WATCHLIST)} coins to Gist", flush=True)
    except Exception as e:
        print(f"Save watchlist error: {e}", flush=True)

# ===== PAPER TRADES =====
def load_paper_trades():
    global PAPER_TRADES
    data = gist_get('paper_trades.json')
    if data:
        # FIX: agar string aa jaye to json me convert kar do
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except:
                data = {}

        if isinstance(data, dict) and 'trades' in data:
            PAPER_TRADES = data['trades']
        elif isinstance(data, dict):
            PAPER_TRADES = data
        else:
            PAPER_TRADES = {}
        print(f"Loaded {len(PAPER_TRADES)} paper trades from Gist", flush=True)
    else:
        PAPER_TRADES = {}

def save_paper_trades():
    data = {'trades': PAPER_TRADES}
    gist_save('paper_trades.json', data)

# ===== LIFETIME PNL =====
def load_total_pnl():
    global total_pnl_lifetime
    data = gist_get('lifetime_pnl.json')
    if data and 'total_pnl' in data:
        total_pnl_lifetime = data['total_pnl']
        print(f"Loaded Lifetime PnL: {total_pnl_lifetime:.2f}%", flush=True)

def save_total_pnl(value):
    global total_pnl_lifetime
    total_pnl_lifetime = value
    data = {'total_pnl': total_pnl_lifetime}
    gist_save('lifetime_pnl.json', data)
    print(f"Lifetime PnL updated to: {total_pnl_lifetime:.2f}%", flush=True)

# ===== TICKER HISTORY LOAD/SAVE =====
def load_ticker_history():
    global TICKER_HISTORY
    data = gist_get('ticker_history.json')
    if data:
        TICKER_HISTORY = data
        print(f"Loaded Ticker History: {len(TICKER_HISTORY)} coins", flush=True)

def save_ticker_history():
    global last_ticker_save
    data_to_save = {k: v for k, v in TICKER_HISTORY.items() if k in WATCHLIST}
    gist_save('ticker_history.json', data_to_save)
    last_ticker_save = time.time()
    print(f"Saved Ticker History: {len(data_to_save)} coins to Gist", flush=True)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram send error: {e}", flush=True)

def process_pump_alert(symbol, change_24h, price):
    if symbol in WATCHLIST:
        WATCHLIST[symbol]['time'] = time.time()
        print(f"Bot1 [CoinDCX]: {symbol} +{change_24h:.2f}% already in watchlist", flush=True)
    else:
        WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        save_watchlist()
        print(f"Bot1 [CoinDCX]: {symbol} +{change_24h:.2f}% added to watchlist, no alert", flush=True)

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].ewm(alpha=1 / period, adjust=False).mean()
    hl2 = (df['high'] + df['low']) / 2
    df['upperband'] = hl2 + (multiplier * df['atr'])
    df['lowerband'] = hl2 - (multiplier * df['atr'])
    df['final_upperband'] = 0.0; df['final_lowerband'] = 0.0
    df['supertrend'] = True; df['st_line'] = 0.0
    for i in range(len(df)):
        if i == 0:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
            df.loc[df.index[i], 'st_line'] = df['upperband'].iloc[i]; continue
        if (df['upperband'].iloc[i] < df['final_upperband'].iloc[i - 1] or df['close'].iloc[i - 1] > df['final_upperband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else: df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i - 1]
        if (df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i - 1] or df['close'].iloc[i - 1] < df['final_lowerband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
        else: df.loc[df.index[i], 'final_lowerband'] = df['final_lowerband'].iloc[i - 1]
        prev_st = df['supertrend'].iloc[i - 1]; close_i = df['close'].iloc[i]
        if prev_st and close_i < df['final_lowerband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = False
        elif not prev_st and close_i > df['upperband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = True
        else: df.loc[df.index[i], 'supertrend'] = prev_st
        if df['supertrend'].iloc[i]: df.loc[df.index[i], 'st_line'] = df['final_lowerband'].iloc[i]
        else: df.loc[df.index[i], 'st_line'] = df['final_upperband'].iloc[i]
    ema_raw = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['ema_val'] = ema_raw.rolling(window=9, min_periods=1).mean()
    return df

# ===== TICKER FALLBACK + GIST SAVE =====
def get_klines_coindcx(symbol, interval='5m', limit=351):
    base = symbol.replace('USDT', '')
    pair = f"{base}-USDT"
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        data = res.json()
        if res.status_code == 200 and data and isinstance(data, list) and len(data) > 50:
            df = pd.DataFrame(data)
            df = df.rename(columns={'time': 'timestamp'})
            df['timestamp'] = df['timestamp'].astype('int64')
            df['open'] = df['open'].astype(float)
            df['high'] = df['high'].astype(float)
            df['low'] = df['low'].astype(float)
            df['close'] = df['close'].astype(float)
            df = df[['timestamp', 'open', 'high', 'low', 'close']]
            df = df.sort_values('timestamp').reset_index(drop=True)
            df = df.iloc[:-1].reset_index(drop=True)
            print(f"Bot2: [{symbol}] Data from CoinDCX API", flush=True)
            return df
    except: pass

    print(f"Bot2: [{symbol}] API fail. Using ticker history fallback", flush=True)
    if symbol not in TICKER_HISTORY or len(TICKER_HISTORY[symbol]) < 50:
        return None

    prices = TICKER_HISTORY[symbol][-limit:]
    df = pd.DataFrame()
    df['close'] = prices
    df['open'] = prices
    df['high'] = prices
    df['low'] = prices
    df['timestamp'] = pd.date_range(end=pd.Timestamp.now(), periods=len(prices), freq='5min').astype('int64') // 10**9
    return df

def get_klines(symbol, interval='5'):
    return get_klines_coindcx(symbol, interval=f"{interval}m")

def check_paper_trades(df, symbol):
    global total_pnl_lifetime
    if symbol not in PAPER_TRADES: return
    trade = PAPER_TRADES[symbol]
    if trade['status']!= 'OPEN': return
    current_price = df['close'].iloc[-1]
    tp = trade['tp']; sl = trade['sl']
    cdcx_name = symbol.replace('USDT', '-USDT')
    entry_time = trade['time']
    if current_price <= tp:
        pnl = ((trade['entry'] - tp) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_TP'; trade['exit'] = tp; trade['pnl'] = round(pnl, 2); trade['exit_time'] = time.time()
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"✅ <b>TRADE CLOSED - TARGET HIT</b> ✅\n\n<b>Coin:</b> {cdcx_name}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit TP:</b> ${tp:.6f}\n<b>PnL:</b> +{pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%\n<b>Duration:</b> {duration} min"
        send_telegram(msg); send_ntfy_plain(msg); print(f"Paper Trade TP: {cdcx_name} +{pnl:.2f}%", flush=True)
    elif current_price >= sl:
        pnl = ((trade['entry'] - sl) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_SL'; trade['exit'] = sl; trade['pnl'] = round(pnl, 2); trade['exit_time'] = time.time()
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"❌ <b>TRADE CLOSED - SL HIT</b> ❌\n\n<b>Coin:</b> {cdcx_name}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit SL:</b> ${sl:.6f}\n<b>PnL:</b> {pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%\n<b>Duration:</b> {duration} min"
        send_telegram(msg); send_ntfy_plain(msg); print(f"Paper Trade SL: {cdcx_name} {pnl:.2f}%", flush=True)
    save_paper_trades()

# ===== ASYNC BOTS =====
async def bot1_scan_coindcx_async():
    global last_ticker_save
    load_watchlist()
    load_ticker_history()
    print("Bot1 started — ONLY CoinDCX FUTURES Scan + Ticker Saver", flush=True)
    while True:
        try:
            url = "https://api.coindcx.com/derivatives/v1/ticker"
            headers = {'User-Agent': 'Mozilla/5.0'}
            res = requests.get(url, headers=headers, timeout=20).json()
            pumped = 0
            for t in res:
                market = t.get('symbol', '')
                if market.endswith('USDT'):
                    base = market.replace('-USDT', '').replace('USDT', '')
                    symbol = f"{base}USDT"
                    price = float(t.get('last_price', '0'))

                    if symbol not in TICKER_HISTORY: TICKER_HISTORY[symbol] = []
                    TICKER_HISTORY[symbol].append(price)
                    if len(TICKER_HISTORY[symbol]) > 1000: TICKER_HISTORY[symbol].pop(0)

                    try:
                        change_str = str(t.get('change24h', '0'))
                        change_24h = float(change_str)
                        if change_24h >= PUMP_PERCENT_24H:
                            process_pump_alert(symbol, change_24h, price)
                            pumped += 1
                    except: continue

            if time.time() - last_ticker_save > 300:
                save_ticker_history()

            print(f"Bot1 [CoinDCX FUTURES]: {len(res)} pairs checked | Pumped: {pumped} | Watchlist: {len(WATCHLIST)}\n", flush=True)
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(300)

async def bot2_supertrend_short_async():
    print("Bot2 started — ONLY CoinDCX Data", flush=True)
    while True:
        global WATCHLIST
        try:
            if not WATCHLIST: print("Bot2: Watchlist empty, 30s wait...", flush=True); await asyncio.sleep(30); continue
            print(f"\nBot2: ===== NEW CYCLE — {len(WATCHLIST)} coins =====", flush=True)
            to_remove = []
            for symbol, info in list(WATCHLIST.items()):
                cdcx_name = symbol.replace('USDT', '-USDT')
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400: print(f"Bot2: [{cdcx_name}] Expire — remove", flush=True); to_remove.append(symbol); continue
                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD + 2:
                    print(f"Bot2: [{cdcx_name}] SKIP — data nahi mila", flush=True)
                    if info.get('data_fail_count', 0) >= 9: to_remove.append(symbol); print(f"Bot2: [{cdcx_name}] 3x fail — remove", flush=True)
                    else: WATCHLIST[symbol]['data_fail_count'] = info.get('data_fail_count', 0) + 1
                    continue
                else: WATCHLIST[symbol]['data_fail_count'] = 0
                try: df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                except Exception as e: print(f"Bot2: [{cdcx_name}] ST error: {e}", flush=True); continue
                st_line = df['st_line'].iloc[-1]; ema_val = df['ema_val'].iloc[-1]; close_price = df['close'].iloc[-1]
                if any(math.isnan(v) for v in [st_line, ema_val, close_price]): continue
                check_paper_trades(df, symbol)
                price_below_st = close_price < st_line; st_below_ema = st_line < ema_val; current_short = price_below_st and st_below_ema
                reset_state = (close_price > st_line) and (st_line > ema_val)
                last_state = info.get('last_state', 'reset'); new_cross = (last_state == 'reset' and current_short == True)
                print(f"Bot2: [{cdcx_name}] Price={close_price:.6f} | ST={st_line:.6f} | EMA={ema_val:.6f} | SHORT={current_short}", flush=True)
                if new_cross:
                    cross_count = info.get('cross_count', 0)
                    if cross_count >= 3: print(f"Bot2: [{cdcx_name}] Limit 3/3 reach — skip", flush=True)
                    else:
                        tp_price = round(close_price * 0.95, 6); sl_price = round(close_price * 1.02, 6)
                        PAPER_TRADES[symbol] = {'entry': close_price, 'tp': tp_price, 'sl': sl_price, 'status': 'OPEN', 'time': time.time()}
                        save_paper_trades()
                        msg = f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n<b>Coin:</b> {cdcx_name}\n<b>Signal:</b> #{cross_count + 1}/3\n<b>Entry:</b> ${close_price:.6f}\n<b>TP 5%:</b> ${tp_price}\n<b>SL 2%:</b> ${sl_price}\n\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
                        send_telegram(msg); send_ntfy_plain(msg)
                        WATCHLIST[symbol]['cross_count'] = cross_count + 1
                        print(f"Bot2: [{cdcx_name}] ✅ PAPER SHORT #{cross_count + 1}", flush=True)
                if current_short: WATCHLIST[symbol]['last_state'] = 'short'
                elif reset_state: WATCHLIST[symbol]['last_state'] = 'reset'
                save_watchlist(); await asyncio.sleep(1)
            for symbol in to_remove: WATCHLIST.pop(symbol, None); save_watchlist()
            print(f"Bot2: ===== CYCLE COMPLETE — 30s wait =====\n", flush=True)
        except Exception as e: import traceback; print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(30)

@app.route('/')
def home():
    open_trades = sum(1 for t in PAPER_TRADES.values() if t['status'] == 'OPEN')
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins. Open Paper Trades: {open_trades}. Lifetime PnL: {total_pnl_lifetime:.2f}%"

@app.route('/watchlist')
def show_watchlist(): return jsonify(WATCHLIST)

@app.route('/papertrades')
def show_papertrades(): return jsonify(PAPER_TRADES)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().upper()
    await update.message.reply_text("Bhai command se baat kar 😅\n\n/ADD BTCUSDT\n/WATCHLIST\n/HELP")

def main():
    global telegram_app
    print(f"BOT_TOKEN set: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID set: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    print(f"GITHUB_TOKEN set: {bool(GITHUB_TOKEN)}", flush=True)

    load_watchlist()
    load_paper_trades()
    load_total_pnl()
    load_ticker_history()

    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("add", add_command))
    telegram_app.add_handler(CommandHandler("remove", remove_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    loop.run_until_complete(telegram_app.bot.delete_webhook(drop_pending_updates=True))
    print("Webhook deleted. Starting Polling...", flush=True)

    loop.create_task(bot1_scan_coindcx_async())
    loop.create_task(bot2_supertrend_short_async())

    flask_thread = threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000, use_reloader=False))
    flask_thread.daemon = True
    flask_thread.start()

    print("Flask started in thread. Running bots with Polling...", flush=True)
    loop.create_task(telegram_app.run_polling(drop_pending_updates=True))
    loop.run_forever()

if __name__ == '__main__':
    main()
