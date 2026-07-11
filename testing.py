import threading
import requests
import time
import os
import json
import asyncio
import pandas as pd
import numpy as np
import math
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)

# ===== CONFIG =====
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
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
total_pnl_lifetime = 0.0
last_ticker_save = 0

# ===== GIST HELPERS =====
def gist_get(filename):
    try:
        res = requests.get(GIST_URL, headers=GIST_HEADERS, timeout=10).json()
        content = res['files'][filename]['content']
        return json.loads(content)
    except Exception as e:
        print(f"Gist GET error {filename}: {e}", flush=True)
        return None

def gist_save(filename, data):
    try:
        payload = {"files": {filename: {"content": json.dumps(data, indent=2)}}}
        requests.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=10)
    except Exception as e:
        print(f"Gist SAVE error {filename}: {e}", flush=True)

def load_watchlist(): # PERMANENT FIX 1: Ganda data auto skip
    global WATCHLIST
    data = gist_get('watchlist.json')
    WATCHLIST = {}
    if data and 'coins' in data:
        for symbol, details in data['coins'].items():
            if isinstance(details, dict): # Sirf dict wale coin load karega
                WATCHLIST[symbol] = details
                WATCHLIST[symbol].setdefault('last_state', 'reset')
                WATCHLIST[symbol].setdefault('prices', [])
                WATCHLIST[symbol].setdefault('cross_count', 0)
            else:
                print(f"Skipping bad data for {symbol}: {details}", flush=True)
    print(f"Loaded {len(WATCHLIST)} coins from Gist", flush=True)

def save_watchlist():
    if not WATCHLIST: return
    data = {'_config': {'pump': PUMP_PERCENT_24H, 'ema': EMA_PERIOD, 'days': WATCHLIST_DAYS}, 'coins': WATCHLIST}
    gist_save('watchlist.json', data)

def load_paper_trades():
    global PAPER_TRADES
    data = gist_get('paper_trades.json')
    if data and 'trades' in data: PAPER_TRADES = data['trades']
    else: PAPER_TRADES = {}
    print(f"Loaded {len(PAPER_TRADES)} paper trades", flush=True)

def save_paper_trades(): gist_save('paper_trades.json', {'trades': PAPER_TRADES})

def load_total_pnl():
    global total_pnl_lifetime
    data = gist_get('lifetime_pnl.json')
    if data and 'total_pnl' in data: total_pnl_lifetime = data['total_pnl']

def save_total_pnl(value):
    global total_pnl_lifetime
    if value == total_pnl_lifetime: return
    total_pnl_lifetime = value
    gist_save('lifetime_pnl.json', {'total_pnl': total_pnl_lifetime})

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try: requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: print(f"Telegram Error: {e}", flush=True)

# ===== TELEGRAM COMMANDS =====
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin = context.args[0].upper() if context.args else ""
    if not coin: return await update.message.reply_text("Use: /add BTCUSDT")
    if not coin.endswith("USDT"): coin += "USDT"
    global WATCHLIST
    if coin not in WATCHLIST:
        WATCHLIST[coin] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset', 'prices': []} # YE SAHI HAI
        save_watchlist()
        await update.message.reply_text(f"✅ {coin} added to watchlist")
    else: await update.message.reply_text(f"⚠️ {coin} already in watchlist")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin = context.args[0].upper() if context.args else ""
    if not coin.endswith("USDT"): coin += "USDT"
    WATCHLIST.pop(coin, None)
    save_watchlist()
    await update.message.reply_text(f"❌ {coin} removed")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not WATCHLIST: return await update.message.reply_text("Watchlist Khali hai")
    coins = "\n".join([f"• {c} | Cross: {WATCHLIST[c]['cross_count']}/3" for c in WATCHLIST.keys()])
    await update.message.reply_text(f"<b>WATCHLIST</b>\n\n{coins}\n\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%", parse_mode="HTML")

# ===== INDICATORS =====
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
    df['final_upperband'] = df['upperband']
    df['final_lowerband'] = df['lowerband']
    df['supertrend'] = True
    df['st_line'] = df['upperband']
    for i in range(1, len(df)):
        if df['upperband'].iloc[i] < df['final_upperband'].iloc[i-1] or df['close'].iloc[i-1] > df['final_upperband'].iloc[i-1]:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else: df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i-1]
        if df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i-1] or df['close'].iloc[i-1] < df['final_lowerband'].iloc[i-1]:
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
        else: df.loc[df.index[i], 'final_lowerband'] = df['final_lowerband'].iloc[i-1]
        if df['supertrend'].iloc[i-1] and df['close'].iloc[i] < df['final_lowerband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = False
        elif not df['supertrend'].iloc[i-1] and df['close'].iloc[i] > df['final_upperband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = True
        else: df.loc[df.index[i], 'supertrend'] = df['supertrend'].iloc[i-1]
        df.loc[df.index[i], 'st_line'] = df['final_lowerband'].iloc[i] if df['supertrend'].iloc[i] else df['final_upperband'].iloc[i]
    df['ema_val'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean().rolling(9).mean()
    return df

def check_paper_trades(df, symbol):
    global total_pnl_lifetime
    if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!= 'OPEN': return
    trade = PAPER_TRADES[symbol]
    current_price = df['close'].iloc[-1]
    if current_price <= trade['tp'] or current_price >= trade['sl']:
        hit_tp = current_price <= trade['tp']
        exit_price = trade['tp'] if hit_tp else trade['sl']
        pnl = ((trade['entry'] - exit_price) / trade['entry']) * 100
        trade['status'] = 'CLOSED_TP' if hit_tp else 'CLOSED_SL'
        trade['pnl'] = round(pnl, 2)
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"{'✅' if hit_tp else '❌'} <b>PAPER TRADE CLOSED</b> {'✅' if hit_tp else '❌'}\n\n<b>Coin:</b> {symbol}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit:</b> ${exit_price:.6f}\n<b>PnL:</b> {pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
        send_telegram(msg)
        save_paper_trades()

# ===== BOT1: PRICE + PUMP SCANNER =====
async def bot1_scan():
    global last_ticker_save
    print("Bot1 started - CoinDCX active_instruments Scanner", flush=True)
    while True:
        try:
            res = requests.get("https://api.coindcx.com/exchange/v1/derivatives/futures/data/active_instruments", timeout=20).json()
            updated = 0
            if isinstance(res, list):
                for t in res:
                    pair = t.get('pair', '')
                    price_str = t.get('ls', '')
                    change_24h = float(t.get('change_24_hour', 0)) * 100
                    if pair and price_str and 'USDT' in pair:
                        symbol = pair.replace('B-','').replace('_','')
                        price = float(price_str)

                        if change_24h >= PUMP_PERCENT_24H and symbol not in WATCHLIST:
                            WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset', 'prices': []}
                            print(f"Bot1: {symbol} +{change_24h:.2f}% added to watchlist", flush=True)

                        if symbol in WATCHLIST:
                            WATCHLIST[symbol]['prices'].append(price)
                            if len(WATCHLIST[symbol]['prices']) > 1000: WATCHLIST[symbol]['prices'].pop(0)
                            updated += 1
            if updated > 0: print(f"Bot1: Updated {updated} symbols", flush=True)
            if time.time() - last_ticker_save > 300: save_watchlist(); last_ticker_save = time.time()
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(300)

# ===== BOT2: SIGNAL ENGINE =====
async def bot2_scan():
    print("Bot2 started - PAPER Signal Engine", flush=True)
    while True:
        try:
            for symbol in list(WATCHLIST.keys()):
                prices = WATCHLIST[symbol].get('prices', [])
                if len(prices) < EMA_PERIOD + 50: continue

                df = pd.DataFrame({'close': prices})
                df['high'] = df['close'].rolling(5).max()
                df['low'] = df['close'].rolling(5).min()
                df = df.dropna()
                if len(df) < EMA_PERIOD + 2: continue

                df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                check_paper_trades(df, symbol)

                st_line = df['st_line'].iloc[-1]
                ema_val = df['ema_val'].iloc[-1]
                close_price = df['close'].iloc[-1]
                if any(math.isnan(v) for v in [st_line, ema_val, close_price]): continue

                price_below_st = close_price < st_line
                st_below_ema = st_line < ema_val
                current_short = price_below_st and st_below_ema
                reset_state = (close_price > st_line) and (st_line > ema_val)
                last_state = WATCHLIST[symbol].get('last_state', 'reset')
                new_cross = (last_state == 'reset' and current_short)

                if new_cross:
                    cross_count = WATCHLIST[symbol].get('cross_count', 0)
                    if cross_count < 3 and symbol not in PAPER_TRADES:
                        tp_price = round(close_price * 0.95, 6)
                        sl_price = round(close_price * 1.02, 6)
                        PAPER_TRADES[symbol] = {'entry': close_price, 'tp': tp_price, 'sl': sl_price, 'status': 'OPEN', 'time': time.time()}
                        save_paper_trades()
                        msg = f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n<b>Coin:</b> {symbol}\n<b>Signal:</b> #{cross_count + 1}/3\n<b>Entry:</b> ${close_price:.6f}\n<b>TP 5%:</b> ${tp_price}\n<b>SL 2%:</b> ${sl_price}\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
                        send_telegram(msg)
                        WATCHLIST[symbol]['cross_count'] += 1

                WATCHLIST[symbol]['last_state'] = 'short' if current_short else 'reset' if reset_state else last_state
                if time.time() - WATCHLIST[symbol]['time'] > WATCHLIST_DAYS * 86400: WATCHLIST.pop(symbol)
                time.sleep(0.5)
            save_watchlist()
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(30)

# ===== FLASK + MAIN =====
@app.route('/')
def home():
    open_trades = sum(1 for t in PAPER_TRADES.values() if t['status'] == 'OPEN')
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins. Open Paper Trades: {open_trades}. Lifetime PnL: {total_pnl_lifetime:.2f}%"

@app.route('/watchlist')
def show_watchlist(): return jsonify(WATCHLIST)

@app.route('/papertrades')
def show_papertrades(): return jsonify(PAPER_TRADES)

def main():
    load_watchlist(); load_paper_trades(); load_total_pnl()
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("add", add_command))
    telegram_app.add_handler(CommandHandler("remove", remove_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(telegram_app.initialize())

    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start() # PERMANENT FIX 2: use_reloader=False

    loop.create_task(bot1_scan())
    loop.create_task(bot2_scan())
    loop.run_until_complete(telegram_app.run_polling())

if __name__ == '__main__': main()