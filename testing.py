import requests
import time
import os
from flask import Flask, jsonify
import threading
import pandas as pd
import numpy as np
import json
import math

app = Flask(__name__)

PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
WATCHLIST_FILE = "watchlist.json"
PAPER_TRADES_FILE = "paper_trades.json"

COINDX_FUTURES = {... tere wala pura set... }

WATCHLIST = {}
PAPER_TRADES = {} # Yaha _TRADES hata ke PAPER_TRADES kar
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")

def load_watchlist():
   ... tera same code...

def save_watchlist():
   ... tera same code...

def load_paper_trades():
    global PAPER_TRADES
    try:
        if os.path.exists(PAPER_TRADES_FILE):
            with open(PAPER_TRADES_FILE, 'r') as f:
                PAPER_TRADES = json.load(f)
                print(f"Loaded {len(PAPER_TRADES)} paper trades", flush=True)
        else:
            PAPER_TRADES = {}
    except Exception as e:
        print(f"Load paper trades error: {e}", flush=True)
        PAPER_TRADES = {}

def save_paper_trades(): # YE FUNCTION ADD KAR
    try:
        with open(PAPER_TRADES_FILE, 'w') as f:
            json.dump(PAPER_TRADES, f)
    except Exception as e:
        print(f"Save paper trades error: {e}", flush=True)

def send_telegram(msg): # YE FUNCTION ADD KAR
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram credentials missing!", flush=True)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        res = requests.post(url, data=data, timeout=10)
        if res.status_code!= 200:
            print(f"Telegram API Error: {res.text}", flush=True)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

# SIRF YE 1 CHECK_PAPER_TRADES RAKH, NECHE WALA DELETE KAR
def check_paper_trades(df, symbol):
    if symbol not in PAPER_TRADES:
        return
    trade = PAPER_TRADES[symbol]
    if trade['status']!= 'OPEN':
        return
    current_price = df['close'].iloc[-1]
    tp = trade['tp']
    sl = trade['sl']
    cdcx_name = symbol.replace('USDT', '-USDT')
    entry_time = trade['time']

    if current_price <= tp:
        pnl = ((trade['entry'] - tp) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_TP'
        trade['exit'] = tp
        trade['pnl'] = round(pnl, 2)
        trade['exit_time'] = time.time()

        msg = (
            f"✅ <b>TRADE CLOSED - TARGET HIT</b> ✅\n\n"
            f"<b>Coin:</b> {cdcx_name}\n"
            f"<b>Entry:</b> ${trade['entry']:.6f}\n"
            f"<b>Exit TP:</b> ${tp:.6f}\n"
            f"<b>PnL:</b> +{pnl:.2f}%\n"
            f"<b>Duration:</b> {duration} min"
        )
        send_telegram(msg)
        print(f"Paper Trade TP: {cdcx_name} +{pnl:.2f}% in {duration}min", flush=True)

    elif current_price >= sl:
        pnl = ((trade['entry'] - sl) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_SL'
        trade['exit'] = sl
        trade['pnl'] = round(pnl, 2)
        trade['exit_time'] = time.time()

        msg = (
            f"❌ <b>TRADE CLOSED - SL HIT</b> ❌\n\n"
            f"<b>Coin:</b> {cdcx_name}\n"
            f"<b>Entry:</b> ${trade['entry']:.6f}\n"
            f"<b>Exit SL:</b> ${sl:.6f}\n"
            f"<b>PnL:</b> {pnl:.2f}%\n"
            f"<b>Duration:</b> {duration} min"
        )
        send_telegram(msg)
        print(f"Paper Trade SL: {cdcx_name} {pnl:.2f}% in {duration}min", flush=True)

    save_paper_trades()

... baaki tera code same...
