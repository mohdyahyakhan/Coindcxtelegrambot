import threading
import asyncio
import httpx
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
from telegram.request import HTTPXRequest

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ===== CONFIGURATION =====
PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300        # 300 EMA
RISK_PER_TRADE = 0.20   # 20% Margin
MAX_OPEN_TRADES = 4     # Max 4 parallel trades
MIN_VOLUME_24H = 2000000

# Risk & Targets
EMERGENCY_SL_PERCENT = 0.020      # 2.0% Max Emergency Hard SL
TARGET_TP_PERCENT = 0.050         # 5.0% Target TP1 (50% Profit Booking)

# CoinDCX Futures Fee Structure (Taker 0.05% + 18% GST)
TAKER_FEE = 0.0005
GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)  # 0.059%

GIST_ID = os.environ.get("GIST_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None

WATCHLIST = {}
PAPER_TRADES = {}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
_lock = asyncio.Lock()

BALANCE_DATA = {
    "total_balance": 10000.0,
    "starting_balance": 10000.0,
    "lifetime_pnl_usdt": 0.0,
    "lifetime_pnl_percent": 0.0
}

# ===== COINDCX SYMBOL MAPPING =====
def get_coindcx_pair(symbol):
    clean_base = symbol.replace('USDT', '').replace('.P', '')
    return f"F-{clean_base}_USDT"

# ===== GIST BACKUP & SYNC =====
async def gist_get(client: httpx.AsyncClient, filename):
    if not GIST_URL or not GITHUB_TOKEN: return {}
    try:
        r = await client.get(GIST_URL, headers=GIST_HEADERS, timeout=10.0)
        if r.status_code != 200: return {}
        gist_data = r.json()
        if filename in gist_data.get('files', {}):
            content = gist_data['files'][filename]['content']
            return json.loads(content) if content else {}
        return {}
    except Exception as e:
        print(f"Gist Get Error ({filename}): {e}", flush=True)
        return {}

async def gist_set(client: httpx.AsyncClient, filename, content):
    if not GIST_URL or not GITHUB_TOKEN: return False
    payload = {"files": {filename: {"content": json.dumps(content, indent=2)}}}
    for attempt in range(3):
        try:
            r = await client.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=15.0)
            if r.status_code == 200: return True
        except Exception as e:
            if attempt == 2:
                print(f"Gist Set Error ({filename}): {e}", flush=True)
            await asyncio.sleep(2)
    return False

async def save_watchlist(client): await gist_set(client, 'watchlist.json', {'coins': WATCHLIST})
async def load_watchlist(client):
    global WATCHLIST
    data = await gist_get(client, 'watchlist.json')
    WATCHLIST = {}
    if data and 'coins' in data:
        for symbol, details in data['coins'].items():
            if isinstance(details, dict):
                clean_sym = symbol.replace('.P', '')
                WATCHLIST[clean_sym] = details
                WATCHLIST[clean_sym].setdefault('last_state', 'reset')
                WATCHLIST[clean_sym].setdefault('attempts', 0)
                WATCHLIST[clean_sym].setdefault('trigger_low', None)
    print(f"Gist Loaded: {len(WATCHLIST)} coins", flush=True)

async def save_paper_trades(client): await gist_set(client, 'paper_trades.json', PAPER_TRADES)
async def load_paper_trades(client):
    global PAPER_TRADES
    PAPER_TRADES = await gist_get(client, 'paper_trades.json') or {}

async def save_balance_data(client): await gist_set(client, 'total_pnl.json', BALANCE_DATA)
async def load_balance_data(client):
    global BALANCE_DATA
    data = await gist_get(client, 'total_pnl.json')
    if data and 'total_balance' in data:
        BALANCE_DATA = data
    else:
        BALANCE_DATA = {
            "total_balance": 10000.0,
            "starting_balance": 10000.0,
            "lifetime_pnl_usdt": 0.0,
            "lifetime_pnl_percent": 0.0
        }
        await save_balance_data(client)

# ===== TELEGRAM NOTIFIER =====
async def send_telegram(client: httpx.AsyncClient, message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    for attempt in range(3):
        try:
            r = await client.post(url, json=payload, timeout=10.0)
            if r.status_code == 200: return
        except Exception as e:
            print(f"Telegram Retry {attempt+1} Error: {e}", flush=True)
            await asyncio.sleep(2)

# ===== TELEGRAM COMMAND HANDLERS =====
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot Active (Strict Low-Lock & Breakout Execution Fix Applied)")

async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        symbol = context.args[0].upper().replace('.P', '')
        async with _lock:
            WATCHLIST[symbol] = {'time': time.time(), 'attempts': 0, 'last_state': 'reset', 'trigger_low': None}
        client = context.bot_data.get("http_client")
        if client: await save_watchlist(client)
        await update.message.reply_text(f"✅ <b>{symbol}</b> added to watchlist (Mapped: <code>{get_coindcx_pair(symbol)}</code>)", parse_mode="HTML")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        symbol = context.args[0].upper().replace('.P', '')
        async with _lock:
            WATCHLIST.pop(symbol, None)
        client = context.bot_data.get("http_client")
        if client: await save_watchlist(client)
        await update.message.reply_text(f"🗑️ <b>{symbol}</b> removed from watchlist", parse_mode="HTML")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = ", ".join(WATCHLIST.keys()) if WATCHLIST else "Empty"
    await update.message.reply_text(f"📋 <b>Watchlist ({len(WATCHLIST)}):</b>\n{coins}", parse_mode="HTML")

async def open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    open_trades_list = {k: v for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN'}
    if not open_trades_list:
        await update.message.reply_text("📊 <b>No Open Trades</b>", parse_mode="HTML")
        return
    msg = f"📊 <b>OPEN TRADES ({len(open_trades_list)}/{MAX_OPEN_TRADES})</b>\n\n"
    for symbol, trade in open_trades_list.items():
        entry = trade['entry']; tp = trade['tp']; sl = trade['sl']; attempt = trade.get('attempt', 1)
        tp1_status = "🎯 (50% TP BOOKED - STEP TRAILING SL ACTIVE)" if trade.get('tp1_hit') else ""
        amount = trade.get('trade_amount_usdt', trade['balance_at_entry'] * RISK_PER_TRADE)
        active_qty = "50%" if trade.get('tp1_hit') else "100%"
        msg += f"<b>{symbol}</b> (CoinDCX: <code>{get_coindcx_pair(symbol)}</code>)\nAttempt: #{attempt}/3 | Entry: <code>${entry:.6f}</code>\nActive Qty: <code>{active_qty}</code> {tp1_status}\nTarget TP1: <code>${tp}</code> | Trailing SL: <code>${sl:.6f}</code>\nMargin: <code>${amount:.2f}</code>\n\n"
    msg += f"<b>Total Balance:</b> ${BALANCE_DATA['total_balance']:.2f}"
    await update.message.reply_text(msg, parse_mode="HTML")

async def close_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Use: /close SYMBOL")
    symbol = context.args[0].upper().replace('.P', '')
    client = context.bot_data.get("http_client")

    async with _lock:
        if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status'] != 'OPEN':
            return await update.message.reply_text(f"❌ {symbol} me koi open trade nahi hai")
        trade = PAPER_TRADES[symbol].copy()

    df = await get_klines(client, symbol)
    if df is None: return await update.message.reply_text(f"❌ {symbol} price fetch failed.")

    exit_price = df['close'].iloc[-1]

    async with _lock:
        tp1_done = trade.get('tp1_hit', False)
        remaining_ratio = 0.5 if tp1_done else 1.0
        trade_amount = trade.get('trade_amount_usdt', trade['balance_at_entry'] * RISK_PER_TRADE) * remaining_ratio

        gross_pnl_percent = ((trade['entry'] - exit_price) / trade['entry']) * 100
        gross_pnl_usdt = trade_amount * (gross_pnl_percent / 100)

        entry_value = trade_amount
        exit_value = trade_amount + gross_pnl_usdt
        entry_fee = entry_value * EFFECTIVE_FEE_RATE
        exit_fee = max(0, exit_value) * EFFECTIVE_FEE_RATE
        total_fees_usdt = entry_fee + exit_fee

        net_pnl_usdt = gross_pnl_usdt - total_fees_usdt
        net_pnl_percent = (net_pnl_usdt / trade_amount) * 100 if trade_amount > 0 else 0

        BALANCE_DATA['total_balance'] += net_pnl_usdt
        BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']
        BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100

        PAPER_TRADES[symbol]['status'] = 'CLOSED_MANUAL'
        PAPER_TRADES[symbol]['pnl_percent'] = round(net_pnl_percent, 2)
        PAPER_TRADES[symbol]['pnl_usdt'] = round(net_pnl_usdt, 2)
        PAPER_TRADES[symbol]['exit_price'] = exit_price
        WATCHLIST.pop(symbol, None)

        if client:
            await save_paper_trades(client)
            await save_balance_data(client)
            await save_watchlist(client)

        msg = f"🔄 <b>MANUAL TRADE CLOSED</b>\n\n<b>Coin:</b> {symbol}\n<b>Closed Qty:</b> {'50%' if tp1_done else '100%'}\n<b>Net PnL:</b> {net_pnl_percent:.2f}% / ${net_pnl_usdt:.2f}\n🗑️ Removed from Watchlist"
        return await update.message.reply_text(msg, parse_mode="HTML")

async def pnl_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bal = BALANCE_DATA
    await update.message.reply_text(
        f"📊 <b>ACCOUNT SUMMARY</b>\n\nStarting: ${bal['starting_balance']:.2f}\nCurrent Balance: ${bal['total_balance']:.2f}\nLifetime PnL: {bal['lifetime_pnl_percent']:.2f}% / ${bal['lifetime_pnl_usdt']:.2f}",
        parse_mode="HTML"
    )

# ===== MARKET DATA & INDICATOR ENGINE =====
async def get_klines_bybit_async(client: httpx.AsyncClient, symbol, interval='5', limit=400):
    url = "https://api.bybit.com/v5/market/kline"
    bybit_symbol = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    params = {'category': 'linear', 'symbol': bybit_symbol, 'interval': interval, 'limit': limit}
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

async def get_klines_coindcx_async(client: httpx.AsyncClient, symbol, interval='5m', limit=400):
    pair = get_coindcx_pair(symbol)
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = await client.get(url, params=params, timeout=10.0)
        data = res.json()
        if not data or not isinstance(data, list): return None
        df = pd.DataFrame(data).rename(columns={'time': 'timestamp'})
        df['timestamp'] = df['timestamp'].astype('int64')
        df[['open', 'high', 'low', 'close']] = df[['open', 'high', 'low', 'close']].astype(float)
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

        if (df['upperband'].iloc[i] < df['final_upperband'].iloc[i - 1] or df['close'].iloc[i - 1] > df['final_upperband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else:
            df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i - 1]

        if (df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i - 1] or df['close'].iloc[i - 1] < df['final_lowerband'].iloc[i - 1]):
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

    # --- TRADINGVIEW MATCH (300 EMA + 9 SMA Smoothing) ---
    ema_300 = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['ema_val'] = ema_300.rolling(window=9, min_periods=1).mean()
    return df

# ===== TRADE MANAGEMENT & EXITS =====
async def check_paper_trades(client, df, symbol):
    global BALANCE_DATA
    async with _lock:
        if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status'] != 'OPEN': return
        trade = PAPER_TRADES[symbol].copy()

    candle_low = df['low'].iloc[-1]
    candle_high = df['high'].iloc[-1]
    candle_close = df['close'].iloc[-1]
    st_line = df['st_line'].iloc[-1]

    # 1. PARTIAL TP1 HIT (-5% TARGET -> CLOSE 50% QUANTITY & SET INITIAL BREAK-EVEN SL)
    if candle_low <= trade['tp'] and not trade.get('tp1_hit'):
        partial_ratio = 0.5
        trade_amount = trade.get('trade_amount_usdt', trade['balance_at_entry'] * RISK_PER_TRADE)
        partial_amount = trade_amount * partial_ratio
        exit_price = trade['tp']

        gross_pnl_percent = ((trade['entry'] - exit_price) / trade['entry']) * 100
        gross_pnl_usdt = partial_amount * (gross_pnl_percent / 100)

        entry_value = partial_amount
        exit_value = partial_amount + gross_pnl_usdt
        entry_fee = entry_value * EFFECTIVE_FEE_RATE
        exit_fee = max(0, exit_value) * EFFECTIVE_FEE_RATE
        total_fees_usdt = entry_fee + exit_fee

        net_pnl_usdt = gross_pnl_usdt - total_fees_usdt
        net_pnl_percent = (net_pnl_usdt / partial_amount) * 100

        async with _lock:
            if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status'] == 'OPEN':
                BALANCE_DATA['total_balance'] += net_pnl_usdt
                BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']
                BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100

                PAPER_TRADES[symbol]['tp1_hit'] = True
                PAPER_TRADES[symbol]['sl'] = trade['entry']  # Shift SL to Break-Even Entry
                PAPER_TRADES[symbol]['lowest_price'] = candle_low
                PAPER_TRADES[symbol]['is_breakeven'] = True

                trade['tp1_hit'] = True
                trade['sl'] = trade['entry']
                trade['lowest_price'] = candle_low
                trade['is_breakeven'] = True

        await save_balance_data(client)
        await save_paper_trades(client)

        msg = (
            f"🎯 <b>50% PARTIAL TP HIT (-5%)</b> 🎯\n\n"
            f"<b>Coin:</b> {symbol} (<code>{get_coindcx_pair(symbol)}</code>)\n"
            f"<b>Booked Profit (50% Qty):</b> {net_pnl_percent:.2f}% / ${net_pnl_usdt:.2f}\n"
            f"<b>Remaining 50% Status:</b> Step-Trailing SL Active\n"
            f"<b>New Initial SL:</b> Break-Even (${trade['entry']:.6f})\n"
            f"<b>Current Balance:</b> ${BALANCE_DATA['total_balance']:.2f}"
        )
        asyncio.create_task(send_telegram(client, msg))

    # 2. STEP DYNAMIC TRAILING SL LOGIC (AFTER TP1 HIT)
    if trade.get('tp1_hit'):
        current_lowest = trade.get('lowest_price', trade['tp'])
        if candle_low < current_lowest:
            new_lowest = candle_low
            new_sl = trade['entry'] - (trade['entry'] - new_lowest) * 0.80  
            
            if new_sl < trade['sl']:
                async with _lock:
                    if symbol in PAPER_TRADES:
                        PAPER_TRADES[symbol]['sl'] = new_sl
                        PAPER_TRADES[symbol]['lowest_price'] = new_lowest
                        trade['sl'] = new_sl
                        trade['lowest_price'] = new_lowest

    # 3. FULL / REMAINING EXIT (SL Hit or Supertrend Exit)
    sl_hit = candle_high >= trade['sl']
    st_exit = candle_close > st_line

    if sl_hit or st_exit:
        attempt_num = trade.get('attempt', 1)
        tp1_done = trade.get('tp1_hit', False)
        remaining_ratio = 0.5 if tp1_done else 1.0

        if sl_hit:
            exit_price = trade['sl']; status_code = 'CLOSED_SL'; status_emoji = '❌' if not tp1_done else '🛡️'
            reason_txt = "Trailing SL Hit" if tp1_done else f"Emergency Max SL Hit ({EMERGENCY_SL_PERCENT*100:.1f}%)"
        else:
            exit_price = candle_close; status_code = 'CLOSED_ST_EXIT'; status_emoji = '🔄'
            reason_txt = "Supertrend Reversal Exit (Runner Closed)" if tp1_done else "Supertrend Reversal Exit"

        remove_from_watchlist = tp1_done or (attempt_num >= 3)

        async with _lock:
            if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status'] != 'OPEN': return

            trade_amount = trade.get('trade_amount_usdt', trade['balance_at_entry'] * RISK_PER_TRADE)
            remaining_amount = trade_amount * remaining_ratio

            gross_pnl_percent = ((trade['entry'] - exit_price) / trade['entry']) * 100
            gross_pnl_usdt = remaining_amount * (gross_pnl_percent / 100)

            entry_value = remaining_amount
            exit_value = remaining_amount + gross_pnl_usdt
            entry_fee = entry_value * EFFECTIVE_FEE_RATE
            exit_fee = max(0, exit_value) * EFFECTIVE_FEE_RATE
            total_fees_usdt = entry_fee + exit_fee

            net_pnl_usdt = gross_pnl_usdt - total_fees_usdt
            net_pnl_percent = (net_pnl_usdt / remaining_amount) * 100 if remaining_amount > 0 else 0

            BALANCE_DATA['total_balance'] += net_pnl_usdt
            trade_snapshot_balance = BALANCE_DATA['total_balance']
            BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']
            BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100

            PAPER_TRADES[symbol]['status'] = status_code
            PAPER_TRADES[symbol]['pnl_percent'] = round(net_pnl_percent, 2)
            PAPER_TRADES[symbol]['pnl_usdt'] = round(net_pnl_usdt, 2)

            if remove_from_watchlist:
                WATCHLIST.pop(symbol, None)
            else:
                if symbol in WATCHLIST:
                    WATCHLIST[symbol]['last_state'] = 'reset'

        await save_balance_data(client)
        await save_paper_trades(client)
        await save_watchlist(client)

        msg = (
            f"{status_emoji} <b>PAPER TRADE FULLY CLOSED</b> {status_emoji}\n\n"
            f"<b>Coin:</b> {symbol} (<code>{get_coindcx_pair(symbol)}</code>)\n"
            f"<b>Reason:</b> {reason_txt}\n"
            f"<b>Closed Portion:</b> {'Remaining 50%' if tp1_done else '100%'}\n"
            f"<b>Exit Price:</b> ${exit_price:.6f}\n"
            f"<b>Portion Net PnL:</b> {net_pnl_percent:.2f}% / ${net_pnl_usdt:.2f}\n"
            f"<b>Total Balance:</b> ${trade_snapshot_balance:.2f}"
        )
        if remove_from_watchlist: msg += f"\n\n🗑️ <b>{symbol} removed from watchlist</b>"
        asyncio.create_task(send_telegram(client, msg))

# ===== SCANNER BOT 1 =====
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
                    if not market.endswith('USDT') and not market.endswith('USDT.P'): continue
                    
                    symbol = market.replace('.P', '')

                    try:
                        change_24h = float(t.get('price24hPcnt', 0)) * 100
                        volume_24h = float(t.get('volume24h', 0))
                        last_price = float(t.get('lastPrice', 0))
                    except: continue

                    if volume_24h < MIN_VOLUME_24H or last_price < 0.001: continue

                    async with _lock:
                        if change_24h >= PUMP_PERCENT_24H and symbol not in WATCHLIST:
                            WATCHLIST[symbol] = {'time': time.time(), 'attempts': 0, 'last_state': 'reset', 'trigger_low': None}
                            added += 1
                            asyncio.create_task(send_telegram(client, f"🚨 <b>40% PUMP DETECTED</b> 🚨\n\n<b>Coin:</b> {symbol}\n<b>CoinDCX Pair:</b> <code>{get_coindcx_pair(symbol)}</code>\n<b>24h Change:</b> +{change_24h:.2f}%\n<b>24h Volume:</b> ${volume_24h:,.0f}"))
            if added > 0: await save_watchlist(client)
        except Exception as e:
            print(f"Bot1 Scan Error: {e}", flush=True)
        await asyncio.sleep(60)

# ===== PROCESS SYMBOL BOT 2 (FIXED ENTRY LOGIC) =====
async def process_symbol(client, symbol):
    df = await get_klines(client, symbol)
    if df is None or len(df) < EMA_PERIOD + 2: return False

    df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
    await check_paper_trades(client, df, symbol)

    st_line = df['st_line'].iloc[-1]
    ema_val = df['ema_val'].iloc[-1]
    close_price = df['close'].iloc[-1]
    candle_low = df['low'].iloc[-1]

    if any(math.isnan(v) for v in [st_line, ema_val, close_price, candle_low]): return False

    watchlist_changed = False
    msg_to_send = None
    new_entry = False

    async with _lock:
        if symbol not in WATCHLIST: return False

        attempts_done = WATCHLIST[symbol].get('attempts', 0)
        last_state = WATCHLIST[symbol].get('last_state', 'reset')
        trigger_low = WATCHLIST[symbol].get('trigger_low', None)

        open_trade = PAPER_TRADES.get(symbol)
        open_trade_exists = open_trade and open_trade.get('status') == 'OPEN'
        active_open_trades = sum(1 for t in PAPER_TRADES.values() if t.get('status') == 'OPEN')

        should_enter = False
        execution_entry_price = None

        # --- STRICT 1st ENTRY LOGIC (STRICT LOCK -> ENTRY ON SUBSEQUENT CANDLE) ---
        if attempts_done == 0:
            # STEP 1: Pehli EMA Breakdown candle par LOW LOCK karo (Usi scan cycle me trade mt lo)
            if trigger_low is None:
                if close_price < ema_val:
                    WATCHLIST[symbol]['trigger_low'] = candle_low
                    watchlist_changed = True

            # STEP 2: Lock pehle se set hai, ab AAGLI candles me check karo ki Low toota ya nahi
            else:
                if candle_low < trigger_low:
                    should_enter = True
                    execution_entry_price = trigger_low  # Exact Locked Low Level (Breakout)
                    WATCHLIST[symbol]['trigger_low'] = None
                    watchlist_changed = True
                
                # Agar Breakout se pehle price wapas EMA ke UPAR close ho jaye -> Lock Reset
                elif close_price > ema_val:
                    WATCHLIST[symbol]['trigger_low'] = None
                    watchlist_changed = True

        # --- 2nd & 3rd ENTRY LOGIC ---
        elif attempts_done in [1, 2]:
            price_below_st = close_price < st_line
            st_below_ema = st_line < ema_val
            if price_below_st and st_below_ema and last_state == 'reset':
                should_enter = True
                execution_entry_price = close_price

        # EXECUTE ENTRY IF CONDITIONS MET
        if should_enter and attempts_done < 3 and not open_trade_exists and active_open_trades < MAX_OPEN_TRADES:
            entry_price = round(execution_entry_price, 6)
            tp_price = round(entry_price * (1 - TARGET_TP_PERCENT), 6)
            sl_price = round(entry_price * (1 + EMERGENCY_SL_PERCENT), 6)
            trade_amount = BALANCE_DATA['total_balance'] * RISK_PER_TRADE

            current_attempt = attempts_done + 1
            WATCHLIST[symbol]['attempts'] = current_attempt
            WATCHLIST[symbol]['last_state'] = 'short'

            PAPER_TRADES[symbol] = {
                'entry': entry_price, 'tp': tp_price, 'sl': sl_price,
                'status': 'OPEN', 'time': time.time(),
                'balance_at_entry': BALANCE_DATA['total_balance'],
                'trade_amount_usdt': trade_amount,
                'attempt': current_attempt,
                'is_breakeven': False,
                'tp1_hit': False
            }

            msg_to_send = (
                f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n"
                f"<b>Coin:</b> {symbol}\n"
                f"<b>CoinDCX Pair:</b> <code>{get_coindcx_pair(symbol)}</code>\n"
                f"<b>Entry Attempt:</b> #{current_attempt}/3\n"
                f"<b>Confirmed Entry Price:</b> ${entry_price:.6f} (Breakout Level)\n"
                f"<b>Margin Amount:</b> ${trade_amount:.2f} (20%)\n"
                f"<b>TP1 Target (-5%):</b> ${tp_price}\n"
                f"<b>Max SL (+2%):</b> ${sl_price}\n"
                f"<b>Runner Exit:</b> Candle Close > Supertrend Line"
            )
            new_entry = True
            watchlist_changed = True

        if close_price > st_line:
            WATCHLIST[symbol]['last_state'] = 'reset'

        has_open_trade = PAPER_TRADES.get(symbol, {}).get('status') == 'OPEN'
        if time.time() - WATCHLIST[symbol]['time'] > WATCHLIST_DAYS * 86400 and not has_open_trade:
            WATCHLIST.pop(symbol, None)
            watchlist_changed = True

    if new_entry:
        await save_paper_trades(client)
        asyncio.create_task(send_telegram(client, msg_to_send))

    return watchlist_changed

async def bot2_scan(client: httpx.AsyncClient):
    print("Bot2: Started", flush=True)
    while True:
        try:
            async with _lock: symbols_to_scan = list(WATCHLIST.keys())
            if not symbols_to_scan:
                await asyncio.sleep(20)
                continue

            tasks = [process_symbol(client, sym) for sym in symbols_to_scan]
            results = await asyncio.gather(*tasks)

            if any(results): await save_watchlist(client)
        except Exception as e:
            print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(15)

# ===== SERVER & MAIN APP =====
@app.route('/')
def home(): return jsonify({"status": "Bot Running", "watchlist_count": len(WATCHLIST)})

async def main_async():
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    async with httpx.AsyncClient(limits=limits) as client:
        await load_watchlist(client)
        await load_paper_trades(client)
        await load_balance_data(client)

        t_request = HTTPXRequest(
            connection_pool_size=20, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0
        )
        telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
        telegram_app.bot_data["http_client"] = client

        for cmd, func in [("start", start_command), ("add", add_command), ("remove", remove_command),
                          ("watchlist", watchlist_command), ("open", open_command),
                          ("close", close_command), ("pnl", pnl_command)]:
            telegram_app.add_handler(CommandHandler(cmd, func))

        await telegram_app.bot.delete_webhook(drop_pending_updates=True)

        port = int(os.environ.get("PORT", 10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()

        asyncio.create_task(bot1_scan(client))
        asyncio.create_task(bot2_scan(client))

        await telegram_app.initialize()
        await telegram_app.start()
        await telegram_app.updater.start_polling(drop_pending_updates=True)

        print("Service operational", flush=True)
        while True: await asyncio.sleep(3600)

def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_async())

if __name__ == '__main__':
    main()