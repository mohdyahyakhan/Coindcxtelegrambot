import threading
import requests
import time
import os
import json
import asyncio
import pandas as pd
import numpy as np
import math
import logging # NAYA ADD
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)

# Flask ke 200 wale log band
log = logging.getLogger('werkzeug') # NAYA ADD
log.setLevel(logging.ERROR) # NAYA ADD

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

#... GIST HELPERS same rahenge...

def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    WATCHLIST = {}
    if data and 'coins' in data:
        for symbol, details in data['coins'].items():
            if isinstance(details, dict):
                WATCHLIST[symbol] = details
                WATCHLIST[symbol].setdefault('last_state', 'reset')
                WATCHLIST[symbol].setdefault('prices', [])
                WATCHLIST[symbol].setdefault('cross_count', 0)
    print(f"Loaded {len(WATCHLIST)} coins", flush=True) # CHOTA KAR DIYA

#... baaki gist functions same...

# ===== TELEGRAM COMMANDS same =====

# ===== INDICATORS same =====

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

# ===== BOT1: CLEAN LOG =====
async def bot1_scan():
    global last_ticker_save
    print("Bot1: Started", flush=True) # CHOTA
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
                            print(f"Bot1: {symbol} +{change_24h:.2f}% added to watchlist", flush=True) # SIRF YE

                        if symbol in WATCHLIST:
                            WATCHLIST[symbol]['prices'].append(price)
                            if len(WATCHLIST[symbol]['prices']) > 1000: WATCHLIST[symbol]['prices'].pop(0)
                            updated += 1
            if updated > 0: print(f"Bot1: Updated {updated} symbols", flush=True) # SIRF YE
            if time.time() - last_ticker_save > 300: save_watchlist(); last_ticker_save = time.time()
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(300)

# ===== BOT2: CLEAN LOG =====
async def bot2_scan():
    print("Bot2: Started", flush=True) # CHOTA
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
                        print(f"📝 PAPER SHORT ENTRY: {symbol} @ {close_price:.4f}") # LOG ME BHI
                        msg = f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n<b>Coin:</b> {symbol}\n<b>Signal:</b> #{cross_count + 1}/3\n<b>Entry:</b> ${close_price:.6f}\n<b>TP 5%:</b> ${tp_price}\n<b>SL 2%:</b> ${sl_price}\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
                        send_telegram(msg)
                        WATCHLIST[symbol]['cross_count'] += 1

                WATCHLIST[symbol]['last_state'] = 'short' if current_short else 'reset' if reset_state else last_state
                if time.time() - WATCHLIST[symbol]['time'] > WATCHLIST_DAYS * 86400: WATCHLIST.pop(symbol)
                time.sleep(0.5)
            save_watchlist()
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(30)

# ===== FLASK + MAIN same =====
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
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()

    loop.create_task(bot1_scan())
    loop.create_task(bot2_scan())
    loop.run_until_complete(telegram_app.run_polling())

if __name__ == '__main__': main()
