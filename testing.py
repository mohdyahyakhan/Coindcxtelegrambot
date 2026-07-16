import nest_asyncio
nest_asyncio.apply()
import threading
import requests
import time
import os
import json
import asyncio
import pandas as pd
import numpy as np
import math
import logging
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ===== CONFIG =====
PUMP_PERCENT_24H = 25 # TESTING KE LIYE 25 KIYA HAI. BAAD ME 40 KAR DENA
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300

GIST_ID = os.environ.get("GIST_ID")
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
        r = requests.get(GIST_URL, headers=GIST_HEADERS, timeout=10)
        r.raise_for_status()
        gist_data = r.json()
        if filename in gist_data.get('files', {}):
            content = gist_data['files'][filename]['content']
            return json.loads(content) if content else {}
        return {}
    except Exception as e:
        print(f"Gist Get Error: {e}", flush=True)
        return {}

def gist_set(filename, content):
    try:
        payload = {"files": {filename: {"content": json.dumps(content, indent=2)}}}
        r = requests.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"Gist Set Error: {e}", flush=True)
        return False

def save_watchlist():
    gist_set('watchlist.json', {'coins': WATCHLIST})

def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    WATCHLIST = {}
    if data and 'coins' in data:
        for symbol, details in data['coins'].items():
            if isinstance(details, dict):
                WATCHLIST[symbol] = details
                WATCHLIST[symbol].setdefault('last_state', 'reset')
                WATCHLIST[symbol].setdefault('cross_count', 0)
                WATCHLIST[symbol].pop('prices', None)
    print(f"Gist Loaded: {len(WATCHLIST)} coins", flush=True) # <-- DEBUG ADD KIYA

def save_paper_trades():
    gist_set('paper_trades.json', PAPER_TRADES)

def load_paper_trades():
    global PAPER_TRADES
    PAPER_TRADES = gist_get('paper_trades.json') or {}

def save_total_pnl(pnl):
    global total_pnl_lifetime
    total_pnl_lifetime = pnl
    gist_set('total_pnl.json', {'pnl': total_pnl_lifetime})

def load_total_pnl():
    global total_pnl_lifetime
    data = gist_get('total_pnl.json')
    total_pnl_lifetime = data.get('pnl', 0.0) if data else 0.0

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=10)
    except: pass

# ===== TELEGRAM COMMANDS =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is Online\nCommands:\n/add SYMBOL\n/remove SYMBOL\n/watchlist\n/pnl")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        symbol = context.args[0].upper()
        WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset'}
        save_watchlist()
        await update.message.reply_text(f"{symbol} added to watchlist")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        symbol = context.args[0].upper()
        WATCHLIST.pop(symbol, None)
        save_watchlist()
        await update.message.reply_text(f"{symbol} removed")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = ", ".join(WATCHLIST.keys()) if WATCHLIST else "Empty"
    await update.message.reply_text(f"Watchlist: {coins}")

async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 <b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%", parse_mode="HTML")

# ===== KLINES =====
def get_klines_bybit(symbol, interval='5', limit=351):
    url = "https://api.bybit.com/v5/market/kline"
    params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res.get('retCode') == 0 and res['result']['list']:
            data = res['result']['list']
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            df = df.astype({'timestamp': 'int64', 'open': float, 'high': float, 'low': float, 'close': float})
            df = df.iloc[::-1].reset_index(drop=True)
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < EMA_PERIOD + 50:
                return None
            return df
    except Exception as e:
        print(f"Bybit Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines_coindcx(symbol, interval='5m', limit=351):
    base = symbol.replace('USDT', '')
    pair = f"F-{base}_USDT"
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        data = res.json()
        if not data or not isinstance(data, list):
            return None
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
        if len(df) < EMA_PERIOD + 50:
            return None
        return df
    except Exception as e:
        print(f"CoinDCX Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines(symbol, interval='5'):
    df = get_klines_bybit(symbol, interval=interval)
    if df is not None:
        return df
    return get_klines_coindcx(symbol, interval=f"{interval}m")

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
    df['final_upperband'] = 0.0
    df['final_lowerband'] = 0.0
    df['supertrend'] = True
    df['st_line'] = 0.0
    for i in range(len(df)):
        if i == 0:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
            df.loc[df.index[i], 'st_line'] = df['upperband'].iloc[i]
            continue
        if (df['upperband'].iloc[i] < df['final_upperband'].iloc[i - 1] or
                df['close'].iloc[i - 1] > df['final_upperband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else:
            df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i - 1]
        if (df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i - 1] or
                df['close'].iloc[i - 1] < df['final_lowerband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
        else:
            df.loc[df.index[i], 'final_lowerband'] = df['final_lowerband'].iloc[i - 1]
        prev_st = df['supertrend'].iloc[i - 1]
        close_i = df['close'].iloc[i]
        if prev_st and close_i < df['final_lowerband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = False
        elif not prev_st and close_i > df['final_upperband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = True
        else:
            df.loc[df.index[i], 'supertrend'] = prev_st
        if df['supertrend'].iloc[i]:
            df.loc[df.index[i], 'st_line'] = df['final_lowerband'].iloc[i]
        else:
            df.loc[df.index[i], 'st_line'] = df['final_upperband'].iloc[i]

    ema_raw = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['ema_val'] = ema_raw.rolling(window=9, min_periods=1).mean()
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
        total_pnl_lifetime += pnl
        trade['status'] = 'CLOSED_TP' if hit_tp else 'CLOSED_SL'
        trade['pnl'] = round(pnl, 2)
        save_total_pnl(total_pnl_lifetime)
        msg = f"{'✅' if hit_tp else '❌'} <b>PAPER TRADE CLOSED</b> {'✅' if hit_tp else '❌'}\n\n<b>Coin:</b> {symbol}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit:</b> ${exit_price:.6f}\n<b>PnL:</b> {pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
        send_telegram(msg)
        save_paper_trades()

# ===== BOT1 =====
async def bot1_scan():
    global last_ticker_save
    print("Bot1: Started", flush=True)
    while True:
        try:
            # COINDCX HATA KE BYBIT LAGAYA
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            res = requests.get(url, timeout=20).json()
            
            added = 0
            top_gainer = None
            top_change = -999
            total_pairs = 0
            
            if res.get('retCode') == 0 and 'list' in res['result']:
                for t in res['result']['list']:
                    market = t.get('symbol', '')
                    if not market.endswith('USDT'): # Sirf USDT pairs
                        continue
                    total_pairs += 1
                    symbol = market # BYBIT pehle se hi BTCUSDT format me hai
                    try:
                        change_24h = float(t.get('price24hPcnt', 0)) * 100 # Bybit % me 0.53 deta hai
                    except (TypeError, ValueError):
                        continue
                    if change_24h > top_change:
                        top_change = change_24h
                        top_gainer = symbol
                    if change_24h >= PUMP_PERCENT_24H and symbol not in WATCHLIST:
                        WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset'}
                        send_telegram(f"🚨 40% PUMP DETECTED 🚨\nCoin: {symbol}\n24h: +{change_24h:.2f}%")
                        print(f"Bot1: {symbol} +{change_24h:.2f}% added to watchlist", flush=True)
                        added += 1
                print(f"Bot1 DEBUG: Total BYBIT-USDT pairs = {total_pairs} | Top gainer = {top_gainer} (+{top_change:.2f}%)", flush=True)
            else:
                print(f"Bot1 DEBUG: Bybit API Error: {res}", flush=True)
                
            if added > 0:
                save_watchlist()
                print(f"Bot1: {added} new coins added this cycle", flush=True)
        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(60)

# ===== BOT2 =====
async def bot2_scan():
    print("Bot2: Started", flush=True)
    while True:
        try:
            if not WATCHLIST:
                await asyncio.sleep(30)
                continue
            for symbol in list(WATCHLIST.keys()):
                try:
                    df = get_klines(symbol)
                except Exception as e:
                    print(f"Bot2: [{symbol}] Kline fetch failed: {e}", flush=True)
                    await asyncio.sleep(5)
                    continue

                if df is None or len(df) < EMA_PERIOD + 2:
                    print(f"Bot2: [{symbol}] SKIP — candle data nahi mila", flush=True)
                    continue
                try:
                    df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                except Exception as e:
                    print(f"Bot2: [{symbol}] ST error: {e}", flush=True)
                    continue

                check_paper_trades(df, symbol)

                st_line = df['st_line'].iloc[-1]
                ema_val = df['ema_val'].iloc[-1]
                close_price = df['close'].iloc[-1]
                if any(math.isnan(v) for v in [st_line, ema_val, close_price]):
                    continue

                price_below_st = close_price < st_line
                st_below_ema = st_line < ema_val
                current_short = price_below_st and st_below_ema
                reset_state = (close_price > st_line) and (st_line > ema_val)
                last_state = WATCHLIST[symbol].get('last_state', 'reset')
                new_cross = (last_state == 'reset' and current_short)

                print(f"Bot2: [{symbol}] Price={close_price:.6f} | ST={st_line:.6f} | EMA{EMA_PERIOD}={ema_val:.6f} | SHORT={current_short}", flush=True)

                if new_cross:
                    cross_count = WATCHLIST[symbol].get('cross_count', 0)
                    if cross_count < 3 and symbol not in PAPER_TRADES:
                        tp_price = round(close_price * 0.95, 6)
                        sl_price = round(close_price * 1.02, 6)
                        PAPER_TRADES[symbol] = {'entry': close_price, 'tp': tp_price, 'sl': sl_price, 'status': 'OPEN', 'time': time.time()}
                        save_paper_trades()
                        print(f"📝 PAPER SHORT ENTRY: {symbol} @ {close_price:.4f}", flush=True)
                        msg = f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n<b>Coin:</b> {symbol}\n<b>Signal:</b> #{cross_count + 1}/3\n<b>Entry:</b> ${close_price:.6f}\n<b>TP 5%:</b> ${tp_price}\n<b>SL 2%:</b> ${sl_price}\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%"
                        send_telegram(msg)
                        WATCHLIST[symbol]['cross_count'] = cross_count + 1

                if current_short:
                    WATCHLIST[symbol]['last_state'] = 'short'
                elif reset_state:
                    WATCHLIST[symbol]['last_state'] = 'reset'

                if time.time() - WATCHLIST[symbol]['time'] > WATCHLIST_DAYS * 86400:
                    WATCHLIST.pop(symbol, None)

                await asyncio.sleep(1)
            save_watchlist()
        except Exception as e:
            print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(30)

# ===== FLASK + MAIN =====
@app.route('/')
def home(): return jsonify({"status": "Bot Running"})

async def main_async():
    load_watchlist(); load_paper_trades(); load_total_pnl()
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("add", add_command))
    telegram_app.add_handler(CommandHandler("remove", remove_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))
    telegram_app.add_handler(CommandHandler("pnl", pnl_command))

    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    await telegram_app.initialize()

    port = int(os.environ.get("PORT", 10000))
    threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()

    asyncio.create_task(bot1_scan())
    asyncio.create_task(bot2_scan())

    print("Your service is live", flush=True)
    await telegram_app.updater.start_polling() # <-- RUN_POLLING HATA KE YE LAGAO
    await telegram_app.updater.idle()

def main():
    asyncio.run(main_async())

if __name__ == '__main__':
    main()