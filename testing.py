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

def send_ntfy_plain(msg):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic: return
    clean_msg = msg.replace('<b>', '').replace('</b>', '').replace('&lt;', '<').replace('&gt;', '>')
    try:
        requests.post(f"https://ntfy.sh/{topic}", data=clean_msg.encode('utf-8'))
    except Exception as e:
        print(f"ntfy Error: {e}", flush=True)

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

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin_text = ""
    if context.args:
        coin_text = context.args[0]
    else:
        coin_text = update.message.text.replace("ADD ", "").replace("add ", "").strip()
    if not coin_text:
        await update.message.reply_text("Use: /add COINNAME\nEx: ADD BTCUSDT")
        return
    coin = coin_text.upper()
    if not coin.endswith("USDT"): coin = coin + "USDT"
    global WATCHLIST
    if coin not in WATCHLIST:
        # FIX: puri WATCHLIST overwrite nahi hogi ab
        WATCHLIST[coin] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'} 
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
        await update.message.reply_text("Use: /remove COINNAME\nEx: REMOVE BTCUSDT")
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
    coins_list = [f"• {coin}" for coin in WATCHLIST.keys()]
    coins = "\n".join(coins_list)
    await update.message.reply_text(f"<b>WATCHLIST - {len(coins_list)} coins</b>\n\n{coins}", parse_mode="HTML")

def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    WATCHLIST = data if data else {}

def save_watchlist():
    gist_save('watchlist.json', WATCHLIST)
    print(f"Saved {len(WATCHLIST)} coins to Gist", flush=True)

def load_paper_trades():
    global PAPER_TRADES
    data = gist_get('paper_trades.json')
    if data:
        if isinstance(data, str):
            try: data = json.loads(data)
            except: data = {}
        PAPER_TRADES = data.get('trades', data) if isinstance(data, dict) else {}
        print(f"Loaded {len(PAPER_TRADES)} paper trades from Gist", flush=True)
    else:
        PAPER_TRADES = {}

def save_paper_trades():
    gist_save('paper_trades.json', {'trades': PAPER_TRADES})

def load_total_pnl():
    global total_pnl_lifetime
    data = gist_get('lifetime_pnl.json')
    if data and 'total_pnl' in data:
        total_pnl_lifetime = data['total_pnl']
        print(f"Loaded Lifetime PnL: {total_pnl_lifetime:.2f}%", flush=True)

def save_total_pnl(value):
    global total_pnl_lifetime
    total_pnl_lifetime = value
    gist_save('lifetime_pnl.json', {'total_pnl': total_pnl_lifetime})

def load_ticker_history():
    global TICKER_HISTORY
    data = gist_get('ticker_history.json')
    TICKER_HISTORY = data if data else {}
    print(f"Loaded Ticker History: {len(TICKER_HISTORY)} coins", flush=True)

def save_ticker_history():
    global last_ticker_save
    data_to_save = {k: v for k, v in TICKER_HISTORY.items() if k in WATCHLIST}
    gist_save('ticker_history.json', data_to_save)
    last_ticker_save = time.time()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': message, 'parse_mode': 'HTML'}, timeout=10)

def process_pump_alert(symbol, change_24h, price):
    if symbol in WATCHLIST:
        WATCHLIST[symbol]['time'] = time.time()
    else:
        WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        save_watchlist()

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

def get_klines_coindcx(symbol, interval='5m', limit=351):
    base = symbol.replace('USDT', '')
    pair = f"{base}-USDT"
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        data = res.json()
        if res.status_code == 200 and data and isinstance(data, list) and len(data) > 50:
            df = pd.DataFrame(data).rename(columns={'time': 'timestamp'})
            for col in ['open','high','low','close']: df[col] = df[col].astype(float)
            df = df[['timestamp', 'open', 'high', 'low', 'close']].sort_values('timestamp').iloc[:-1].reset_index(drop=True)
            return df
    except: pass
    if symbol not in TICKER_HISTORY or len(TICKER_HISTORY[symbol]) < 50:
        return None
    prices = TICKER_HISTORY[symbol][-limit:]
    df = pd.DataFrame({'close': prices, 'open': prices, 'high': prices, 'low': prices})
    df['timestamp'] = pd.date_range(end=pd.Timestamp.now(), periods=len(prices), freq='5min').astype('int64') // 10**9
    return df

def get_klines(symbol, interval='5'):
    return get_klines_coindcx(symbol, interval=f"{interval}m")

def check_paper_trades(df, symbol):
    global total_pnl_lifetime
    if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!= 'OPEN': return
    trade = PAPER_TRADES[symbol]
    current_price = df['close'].iloc[-1]
    tp = trade['tp']; sl = trade['sl']
    if current_price <= tp:
        pnl = ((trade['entry'] - tp) / trade['entry']) * 100
        trade['status'] = 'CLOSED_TP'; trade['exit'] = tp; trade['pnl'] = round(pnl, 2)
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"✅ <b>TP HIT</b> ✅\n<b>Coin:</b> {symbol}\n<b>PnL:</b> +{pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
        send_telegram(msg); send_ntfy_plain(msg)
    elif current_price >= sl:
        pnl = ((trade['entry'] - sl) / trade['entry']) * 100
        trade['status'] = 'CLOSED_SL'; trade['exit'] = sl; trade['pnl'] = round(pnl, 2)
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"❌ <b>SL HIT</b> ❌\n<b>Coin:</b> {symbol}\n<b>PnL:</b> {pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
        send_telegram(msg); send_ntfy_plain(msg)
    save_paper_trades()

async def bot1_scan_coindcx_async():
    global last_ticker_save
    load_watchlist(); load_ticker_history()
    print("Bot1 started", flush=True)
    while True:
        try:
            res = requests.get("https://api.coindcx.com/derivatives/v1/ticker", timeout=20).json()
            for t in res:
                market = t.get('symbol', '')
                if market.endswith('USDT'):
                    symbol = f"{market.replace('-USDT', '')}USDT"
                    price = float(t.get('last_price', '0'))
                    if symbol not in TICKER_HISTORY: TICKER_HISTORY[symbol] = []
                    TICKER_HISTORY[symbol].append(price)
                    if len(TICKER_HISTORY[symbol]) > 1000: TICKER_HISTORY[symbol].pop(0)
                    try:
                        if float(t.get('change24h', '0')) >= PUMP_PERCENT_24H:
                            process_pump_alert(symbol, float(t.get('change24h', '0')), price)
                    except: continue
            if time.time() - last_ticker_save > 300: save_ticker_history()
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(300)

async def bot2_supertrend_short_async():
    print("Bot2 started", flush=True)
    while True:
        global WATCHLIST
        try:
            if not WATCHLIST: await asyncio.sleep(30); continue
            for symbol, info in list(WATCHLIST.items()):
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400: WATCHLIST.pop(symbol); continue
                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD + 2: continue
                df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                st_line = df['st_line'].iloc[-1]; ema_val = df['ema_val'].iloc[-1]; close_price = df['close'].iloc[-1]
                check_paper_trades(df, symbol)
                current_short = (close_price < st_line) and (st_line < ema_val)
                reset_state = (close_price > st_line) and (st_line > ema_val)
                if info.get('last_state', 'reset') == 'reset' and current_short:
                    tp_price = round(close_price * 0.95, 6); sl_price = round(close_price * 1.02, 6)
                    PAPER_TRADES[symbol] = {'entry': close_price, 'tp': tp_price, 'sl': sl_price, 'status': 'OPEN', 'time': time.time()}
                    save_paper_trades()
                    send_telegram(f"📝 <b>PAPER SHORT</b> 📝\n<b>Coin:</b> {symbol}\n<b>Entry:</b> ${close_price:.6f}")
                WATCHLIST[symbol]['last_state'] = 'short' if current_short else 'reset' if reset_state else info.get('last_state')
                save_watchlist(); await asyncio.sleep(1)
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(30)

@app.route('/')
def home():
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins. Lifetime PnL: {total_pnl_lifetime:.2f}%"

def main():
    global telegram_app
    load_watchlist(); load_paper_trades(); load_total_pnl(); load_ticker_history()
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("add", add_command))
    telegram_app.add_handler(CommandHandler("remove", remove_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.bot.delete_webhook(drop_pending_updates=True))
    loop.create_task(bot1_scan_coindcx_async())
    loop.create_task(bot2_supertrend_short_async())
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=10000), daemon=True).start()
    loop.create_task(telegram_app.run_polling(drop_pending_updates=True))
    loop.run_forever()

if __name__ == '__main__':
    main()
