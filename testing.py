import threading
import asyncio
import httpx # requests ki jagah
import time
import os
import json
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
PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
RISK_PER_TRADE = 0.10
MIN_VOLUME_24H = 2000000

GIST_ID = os.environ.get("GIST_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None

WATCHLIST = {}
PAPER_TRADES = {}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
_lock = asyncio.Lock()

# ===== NEW BALANCE SYSTEM =====
BALANCE_DATA = {"total_balance": 10000.0, "starting_balance": 10000.0, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}

# ===== GIST HELPERS - httpx se fast =====
async def gist_get(client: httpx.AsyncClient, filename):
    if not GIST_URL or not GITHUB_TOKEN: return {}
    try:
        r = await client.get(GIST_URL, headers=GIST_HEADERS, timeout=10.0)
        if r.status_code!= 200: return {}
        gist_data = r.json()
        if filename in gist_data.get('files', {}):
            content = gist_data['files'][filename]['content']
            return json.loads(content) if content else {}
        return {}
    except Exception as e: print(f"Gist Get Error: {e}", flush=True); return {}

async def gist_set(client: httpx.AsyncClient, filename, content):
    if not GIST_URL or not GITHUB_TOKEN: return False
    try:
        payload = {"files": {filename: {"content": json.dumps(content, indent=2)}}}
        r = await client.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=10.0)
        return r.status_code == 200
    except Exception as e: print(f"Gist Set Error: {e}", flush=True); return False

async def save_watchlist(client): await gist_set(client, 'watchlist.json', {'coins': WATCHLIST})
async def load_watchlist(client):
    global WATCHLIST
    data = await gist_get(client, 'watchlist.json')
    WATCHLIST = {}
    if data and 'coins' in data:
        for symbol, details in data['coins'].items():
            if isinstance(details, dict):
                WATCHLIST[symbol] = details
                WATCHLIST[symbol].setdefault('last_state', 'reset')
                WATCHLIST[symbol].setdefault('cross_count', 0)
    print(f"Gist Loaded: {len(WATCHLIST)} coins", flush=True)

async def save_paper_trades(client): await gist_set(client, 'paper_trades.json', PAPER_TRADES)
async def load_paper_trades(client):
    global PAPER_TRADES; PAPER_TRADES = await gist_get(client, 'paper_trades.json') or {}

async def save_balance_data(client): await gist_set(client, 'total_pnl.json', BALANCE_DATA)
async def load_balance_data(client):
    global BALANCE_DATA
    data = await gist_get(client, 'total_pnl.json')
    if data and 'total_balance' in data: BALANCE_DATA = data
    else:
        BALANCE_DATA = {"total_balance": 10000.0, "starting_balance": 10000.0, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
        await save_balance_data(client)

async def send_telegram(client: httpx.AsyncClient, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try: await client.post(url, json=payload, timeout=10.0)
    except: pass

# ===== TELEGRAM COMMANDS - same =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot is Online\nCommands:\n/add SYMBOL\n/remove SYMBOL\n/watchlist\n/open\n/close SYMBOL\n/pnl")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        symbol = context.args[0].upper()
        async with _lock: WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset'}
        await update.message.reply_text(f"{symbol} added to watchlist")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        symbol = context.args[0].upper()
        async with _lock: WATCHLIST.pop(symbol, None)
        await update.message.reply_text(f"{symbol} removed")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = ", ".join(WATCHLIST.keys()) if WATCHLIST else "Empty"
    await update.message.reply_text(f"Watchlist: {coins}")

async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_trades_list = {k:v for k,v in PAPER_TRADES.items() if v.get('status') == 'OPEN'}
    if not open_trades_list:
        await update.message.reply_text("📊 <b>No Open Trades</b>\n\nAbhi koi active paper trade nahi hai", parse_mode="HTML")
        return
    msg = "📊 <b>OPEN TRADES</b>\n\n"
    for symbol, trade in open_trades_list.items():
        entry = trade['entry']; tp = trade['tp']; sl = trade['sl']; attempt = trade.get('attempt',1)
        amount = trade['balance_at_entry'] * RISK_PER_TRADE
        msg += f"<b>{symbol}</b> | SHORT | Attempt #{attempt}/3\nEntry: <code>${entry:.6f}</code>\nTP: <code>${tp}</code> | SL: <code>${sl}</code>\nAmount: <code>${amount:.2f}</code>\n\n"
    msg += f"<b>Total Balance:</b> ${BALANCE_DATA['total_balance']:.2f}"
    await update.message.reply_text(msg, parse_mode="HTML")

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Use: /close SYMBOL")
    symbol = context.args[0].upper()
    async with _lock:
        if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status'] == 'OPEN':
            PAPER_TRADES[symbol]['status'] = 'CLOSED_MANUAL'
            WATCHLIST.pop(symbol, None)
            return await update.message.reply_text(f"✅ {symbol} trade manually closed & removed")
    await update.message.reply_text(f"{symbol} me koi open trade nahi hai")

async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = BALANCE_DATA
    await update.message.reply_text(f"📊 <b>ACCOUNT SUMMARY</b>\n\n<b>Starting Balance:</b> ${bal['starting_balance']:.2f}\n<b>Current Balance:</b> ${bal['total_balance']:.2f}\n<b>Lifetime PnL:</b> {bal['lifetime_pnl_percent']:.2f}% / ${bal['lifetime_pnl_usdt']:.2f}", parse_mode="HTML")

# ===== KLINES + INDICATORS - ASYNC VERSION =====
async def get_klines_bybit_async(client: httpx.AsyncClient, symbol, interval='5', limit=351):
    url = "https://api.bybit.com/v5/market/kline"
    params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        res = await client.get(url, params=params, timeout=10.0)
        data = res.json()
        if data.get('retCode') == 0 and data['result']['list']:
            df = pd.DataFrame(data['result']['list'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            df = df.astype({'timestamp': 'int64', 'open': float, 'high': float, 'low': float, 'close': float})
            df = df.iloc[::-1].reset_index(drop=True).iloc[:-1].reset_index(drop=True)
            if len(df) < EMA_PERIOD + 50: return None
            return df
    except: pass
    return None

async def get_klines_coindcx_async(client: httpx.AsyncClient, symbol, interval='5m', limit=351):
    base = symbol.replace('USDT', '')
    pair = f"F-{base}_USDT"
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = await client.get(url, params=params, timeout=10.0)
        data = res.json()
        if not data or not isinstance(data, list): return None
        df = pd.DataFrame(data).rename(columns={'time': 'timestamp'})
        df['timestamp'] = df['timestamp'].astype('int64')
        df[['open','high','low','close']] = df[['open','high','low','close']].astype(float)
        df = df[['timestamp', 'open', 'high', 'low', 'close']].sort_values('timestamp').reset_index(drop=True).iloc[:-1].reset_index(drop=True)
        if len(df) < EMA_PERIOD + 50: return None
        return df
    except: pass
    return None

async def get_klines(client, symbol):
    df = await get_klines_bybit_async(client, symbol)
    if df is not None: return df
    return await get_klines_coindcx_async(client, symbol)

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()
    df['h-l'] = df['high'] - df['low']; df['h-pc'] = abs(df['high'] - df['close'].shift(1)); df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].ewm(alpha=1 / period, adjust=False).mean()
    hl2 = (df['high'] + df['low']) / 2
    df['upperband'] = hl2 + (multiplier * df['atr']); df['lowerband'] = hl2 - (multiplier * df['atr'])
    df['final_upperband'] = 0.0; df['final_lowerband'] = 0.0; df['supertrend'] = True; df['st_line'] = 0.0
    for i in range(len(df)):
        if i == 0:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]; df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]; df.loc[df.index[i], 'st_line'] = df['upperband'].iloc[i]; continue
        if (df['upperband'].iloc[i] < df['final_upperband'].iloc[i - 1] or df['close'].iloc[i - 1] > df['final_upperband'].iloc[i - 1]): df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else: df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i - 1]
        if (df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i - 1] or df['close'].iloc[i - 1] < df['final_lowerband'].iloc[i - 1]): df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
        else: df.loc[df.index[i], 'final_lowerband'] = df['final_lowerband'].iloc[i - 1]
        prev_st = df['supertrend'].iloc[i - 1]; close_i = df['close'].iloc[i]
        if prev_st and close_i < df['final_lowerband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = False
        elif not prev_st and close_i > df['final_upperband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = True
        else: df.loc[df.index[i], 'supertrend'] = prev_st
        if df['supertrend'].iloc[i]: df.loc[df.index[i], 'st_line'] = df['final_lowerband'].iloc[i]
        else: df.loc[df.index[i], 'st_line'] = df['final_upperband'].iloc[i]
    ema_raw = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['ema_val'] = ema_raw.rolling(window=9, min_periods=1).mean()
    return df

# ===== STRATEGY LOGIC - SAME =====
async def check_paper_trades(client, df, symbol):
    global BALANCE_DATA
    async with _lock:
        if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!= 'OPEN': return
        trade = PAPER_TRADES[symbol]

    candle_low = df['low'].iloc[-1]; candle_high = df['high'].iloc[-1]
    tp_hit = candle_low <= trade['tp']; sl_hit = candle_high >= trade['sl']

    if tp_hit or sl_hit:
        exit_price = trade['tp'] if tp_hit else trade['sl']
        remove_from_watchlist = False
        async with _lock:
            trade_pnl_percent = ((trade['entry'] - exit_price) / trade['entry']) * 100
            trade_amount = trade['balance_at_entry'] * RISK_PER_TRADE; trade_pnl_usdt = trade_amount * (trade_pnl_percent / 100)
            BALANCE_DATA['total_balance'] += trade_pnl_usdt
            BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']
            BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100

            if tp_hit:
                PAPER_TRADES[symbol]['status'] = 'CLOSED_TP'; remove_from_watchlist = True; status_emoji = '✅'
            else:
                PAPER_TRADES[symbol]['status'] = 'CLOSED_SL'; status_emoji = '❌'
                if PAPER_TRADES[symbol].get('attempt', 1) >= 3: remove_from_watchlist = True

            PAPER_TRADES[symbol]['pnl_percent'] = round(trade_pnl_percent, 2); PAPER_TRADES[symbol]['pnl_usdt'] = round(trade_pnl_usdt, 2)
            if remove_from_watchlist: WATCHLIST.pop(symbol, None)

        await save_balance_data(client); await save_paper_trades(client)
        if remove_from_watchlist: await save_watchlist(client)

        msg = f"{status_emoji} <b>PAPER TRADE CLOSED</b> {status_emoji}\n\n<b>Coin:</b> {symbol}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit:</b> ${exit_price:.6f}\n<b>Attempt:</b> #{trade.get('attempt',1)}/3\n<b>Trade PnL:</b> {trade_pnl_percent:.2f}% / ${trade_pnl_usdt:.2f}\n<b>New Balance:</b> ${BALANCE_DATA['total_balance']:.2f}"
        if remove_from_watchlist: msg += f"\n\n🗑️ <b>{symbol} removed from watchlist</b>"
        await send_telegram(client, msg)

# ===== BOT1: PUMP SCANNER - ASYNC =====
async def bot1_scan(client: httpx.AsyncClient):
    print("Bot1: Started", flush=True)
    while True:
        try:
            url = "https://api.bybit.com/v5/market/tickers?category=linear"
            res = await client.get(url, timeout=20.0)
            data = res.json()
            added = 0
            if data.get('retCode') == 0 and data.get('result'):
                for t in data['result']['list']:
                    market = t.get('symbol', '')
                    if not market.endswith('USDT'): continue
                    symbol = market
                    try:
                        change_24h = float(t.get('price24hPcnt', 0)) * 100 # Bybit decimal hai
                        volume_24h = float(t.get('volume24h', 0)); last_price = float(t.get('lastPrice', 0))
                    except: continue
                    if volume_24h < MIN_VOLUME_24H or last_price < 0.001 or '.P' in symbol: continue
                    async with _lock:
                        if change_24h >= PUMP_PERCENT_24H and symbol not in WATCHLIST:
                            WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'reset'}; added += 1
                            await send_telegram(client, f"🚨 <b>40% PUMP DETECTED</b> 🚨\n\n<b>Coin:</b> {symbol}\n<b>24h:</b> +{change_24h:.2f}%\n<b>Volume:</b> ${volume_24h:,.0f}")
            if added > 0: await save_watchlist(client)
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(60)

# ===== BOT2: STRATEGY SCANNER - PARALLEL WITH GATHER =====
async def process_symbol(client, symbol):
    df = await get_klines(client, symbol)
    if df is None: return False
    if len(df) < EMA_PERIOD + 2: return False

    df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
    await check_paper_trades(client, df, symbol)

    st_line = df['st_line'].iloc[-1]; ema_val = df['ema_val'].iloc[-1]; close_price = df['close'].iloc[-1]
    if any(math.isnan(v) for v in [st_line, ema_val, close_price]): return False

    watchlist_changed = False
    async with _lock:
        if symbol not in WATCHLIST: return False
        price_below_st = close_price < st_line; st_below_ema = st_line < ema_val; current_short = price_below_st and st_below_ema
        reset_state = (close_price > st_line)
        last_state = WATCHLIST[symbol].get('last_state', 'reset'); new_cross = (last_state == 'reset' and current_short)
        open_trade = PAPER_TRADES.get(symbol)
        open_trade_exists = open_trade and open_trade.get('status') == 'OPEN'
        attempt = open_trade.get('attempt', 0) if open_trade else 0

        if new_cross and attempt < 3 and not open_trade_exists:
            tp_price = round(close_price * 0.95, 6); sl_price = round(close_price * 1.02, 6)
            PAPER_TRADES[symbol] = {'entry': close_price, 'tp': tp_price, 'sl': sl_price, 'status': 'OPEN', 'time': time.time(), 'balance_at_entry': BALANCE_DATA['total_balance'], 'attempt': attempt + 1}
            trade_amount = BALANCE_DATA['total_balance'] * RISK_PER_TRADE
            msg = f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n<b>Coin:</b> {symbol}\n<b>Attempt:</b> #{attempt + 1}/3\n<b>Entry:</b> ${close_price:.6f}\n<b>Amount:</b> ${trade_amount:.2f} (10%)\n<b>TP:</b> ${tp_price} | <b>SL:</b> ${sl_price}"
            await send_telegram(client, msg); watchlist_changed = True

        if current_short: WATCHLIST[symbol]['last_state'] = 'short'
        elif reset_state: WATCHLIST[symbol]['last_state'] = 'reset'

        has_open_trade = PAPER_TRADES.get(symbol, {}).get('status') == 'OPEN'
        if time.time() - WATCHLIST[symbol]['time'] > WATCHLIST_DAYS * 86400 and not has_open_trade:
            WATCHLIST.pop(symbol, None); watchlist_changed = True
    return watchlist_changed

async def bot2_scan(client: httpx.AsyncClient):
    print("Bot2: Started", flush=True)
    while True:
        try:
            async with _lock: symbols_to_scan = list(WATCHLIST.keys())
            if not symbols_to_scan: await asyncio.sleep(30); continue

            # YAHI MAGIC HAI: SAARE COINS EK SAATH
            tasks = [process_symbol(client, sym) for sym in symbols_to_scan]
            results = await asyncio.gather(*tasks)

            if any(results): await save_watchlist(client)
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(15) # 30sec se 15sec kar diya

# ===== FLASK + MAIN =====
@app.route('/')
def home(): return jsonify({"status": "Bot Running"})

async def main_async():
    async with httpx.AsyncClient() as client: # Ek hi client saare kaam ke liye
        print("DEBUG: ALL KEYS SET", flush=True)
        await load_watchlist(client); await load_paper_trades(client); await load_balance_data(client)

        telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
        for cmd, func in [("start", start_command), ("add", add_command), ("remove", remove_command), ("watchlist", watchlist_command), ("open", open_command), ("close", close_command), ("pnl", pnl_command)]:
            telegram_app.add_handler(CommandHandler(cmd, func))
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)

        port = int(os.environ.get("PORT", 10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()

        asyncio.create_task(bot1_scan(client)); asyncio.create_task(bot2_scan(client))

        await telegram_app.initialize(); await telegram_app.start(); await telegram_app.updater.start_polling(drop_pending_updates=True)
        print("Your service is live", flush=True)
        while True: await asyncio.sleep(3600)

def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_async())

if __name__ == '__main__': main()