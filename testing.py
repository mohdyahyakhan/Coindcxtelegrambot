import requests
import time
import os
from flask import Flask, jsonify
import threading
import pandas as pd
import numpy as np
import json
import math
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

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
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
total_pnl_lifetime = 0.0
telegram_app = None # Telegram bot instance

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

# ===== NEW: TELEGRAM COMMANDS FOR WATCHLIST =====
async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /add BTCUSDT """
    if not context.args:
        await update.message.reply_text("Use: /add COINNAME\nEx: /add BTCUSDT")
        return

    coin = context.args[0].upper()
    if not coin.endswith("USDT"):
        coin = coin + "USDT"

    global WATCHLIST
    if coin not in WATCHLIST:
        WATCHLIST[coin] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        save_watchlist() # Seedha Gist mein save
        await update.message.reply_text(f"✅ {coin} ko WATCHLIST me add kar diya")
    else:
        await update.message.reply_text(f"⚠️ {coin} pehle se WATCHLIST me hai")

async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /remove BTCUSDT """
    if not context.args:
        await update.message.reply_text("Use: /remove COINNAME\nEx: /remove BTCUSDT")
        return

    coin = context.args[0].upper()
    if not coin.endswith("USDT"):
        coin = coin + "USDT"

    global WATCHLIST
    if coin in WATCHLIST:
        WATCHLIST.pop(coin)
        save_watchlist() # Seedha Gist se hata
        await update.message.reply_text(f"❌ {coin} ko WATCHLIST se hata diya")
    else:
        await update.message.reply_text(f"⚠️ {coin} WATCHLIST me hai hi nahi")

async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ /watchlist """
    if not WATCHLIST:
        await update.message.reply_text("WATCHLIST khali hai")
        return
    coins = "\n".join([f"• {k}" for k in WATCHLIST.keys()])
    await update.message.reply_text(f"<b>WATCHLIST - {len(WATCHLIST)} coins</b>\n\n{coins}", parse_mode="HTML")

# ===== COINDX FUTURES AUTO FETCH =====
def get_coindcx_futures_symbols():
    try:
        url = "https://api.coindcx.com/exchange/ticker"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=20)
        data = res.json()
        print(f"Bot1 Debug: CoinDCX returned {len(data)} tickers", flush=True)
        futures_symbols = set()
        for t in data:
            market = t.get('market', '')
            if market.endswith('USDT') and not market.startswith('B-'):
                base = market.replace('_USDT', '').replace('USDT', '')
                symbol = f"{base}USDT"
                futures_symbols.add(symbol)
        print(f"Bot1: CoinDCX se {len(futures_symbols)} USDT pairs mile", flush=True)
        return futures_symbols
    except Exception as e:
        print(f"Bot1: CoinDCX futures list error: {e}", flush=True)
        return set()

# ===== WATCHLIST - FINAL FIX =====
def load_watchlist():
    global WATCHLIST
    data = gist_get('watchlist.json')
    if data and isinstance(data, list):
        WATCHLIST = {}
        for symbol in data:
            WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        print(f"Gist watchlist loaded: {len(WATCHLIST)} coins", flush=True)
    else:
        print("Gist watchlist empty or failed, keeping memory watchlist", flush=True)

def save_watchlist():
    if not WATCHLIST:
        print("Watchlist empty, skipping Gist save to prevent overwrite", flush=True)
        return
    try:
        symbol_list = list(WATCHLIST.keys())
        gist_save('watchlist.json', symbol_list)
        print(f"Saved {len(symbol_list)} coins to Gist", flush=True)
    except Exception as e:
        print(f"Save watchlist error: {e}", flush=True)

# ===== PAPER TRADES =====
def load_paper_trades():
    global PAPER_TRADES
    data = gist_get('paper_trades.json')
    if data and 'trades' in data:
        PAPER_TRADES = data['trades']
        print(f"Loaded {len(PAPER_TRADES)} paper trades from Gist", flush=True)
    else:
        print("Gist paper_trades empty or failed, keeping memory", flush=True)

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
    else:
        print("Gist PnL empty or failed, keeping memory value", flush=True)

def save_total_pnl(value):
    global total_pnl_lifetime
    current_gist = gist_get('lifetime_pnl.json')
    if current_gist and 'total_pnl' in current_gist:
        total_pnl_lifetime = max(current_gist['total_pnl'], value)
    else:
        total_pnl_lifetime = value
    data = {'total_pnl': total_pnl_lifetime}
    gist_save('lifetime_pnl.json', data)

def send_telegram(msg):
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

def process_pump_alert(symbol, change_24h, price, source, alerted_symbols):
    if symbol in alerted_symbols: return
    alerted_symbols.add(symbol)
    cdcx_name = symbol.replace('USDT', '-USDT')
    if symbol not in WATCHLIST:
        WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        save_watchlist()
        print(f"Bot1 [{source}]: {cdcx_name} +{change_24h:.2f}% added to watchlist, no TG alert", flush=True)
    else:
        WATCHLIST[symbol]['time'] = time.time()
        save_watchlist()
        print(f"Bot1 [{source}]: {cdcx_name} +{change_24h:.2f}% still pumping, already in watchlist", flush=True)

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
        if prev_st and close_i < df['final_lowerband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = False
        elif not prev_st and close_i > df['final_upperband'].iloc[i]: df.loc[df.index[i], 'supertrend'] = True
        else: df.loc[df.index[i], 'supertrend'] = prev_st
        if df['supertrend'].iloc[i]: df.loc[df.index[i], 'st_line'] = df['final_lowerband'].iloc[i]
        else: df.loc[df.index[i], 'st_line'] = df['final_upperband'].iloc[i]
    ema_raw = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['ema_val'] = ema_raw.rolling(window=9, min_periods=1).mean()
    return df

def get_klines_bybit(symbol, interval='5', limit=351):
    url = "https://api.bybit.com/v5/market/kline"
    params = {'category': 'linear', 'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res['retCode'] == 0 and res['result']['list']:
            data = res['result']['list']
            if len(data) == 0: return None
            df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            df = df.astype({'timestamp': 'int64', 'open': float, 'high': float, 'low': float, 'close': float})
            df = df.iloc[::-1].reset_index(drop=True)
            df = df.iloc[:-1].reset_index(drop=True)
            if len(df) < EMA_PERIOD + 50: return None
            return df
    except Exception as e: print(f"Bybit Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines_coindcx(symbol, interval='5m', limit=351):
    base = symbol.replace('USDT', '')
    pair = f"{base}USDT"
    url = "https://api.coindcx.com/exchange/v1/candles"
    params = {'pair': pair, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        if res.status_code!= 200: return None
        data = res.json()
        if not data or not isinstance(data, list): return None
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
        if len(df) < EMA_PERIOD + 50: return None
        return df
    except Exception as e: print(f"CoinDCX Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines_binance(symbol, interval='5m', limit=351):
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {'symbol': symbol, 'interval': interval, 'limit': limit}
    try:
        res = requests.get(url, params=params, timeout=15)
        if res.status_code!= 200: return None
        data = res.json()
        if not data: return None
        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume','close_time', 'quote_volume', 'trades', 'taker_buy_base','taker_buy_quote', 'ignore'])
        df = df[['timestamp', 'open', 'high', 'low', 'close']]
        df = df.astype({'timestamp': 'int64', 'open': float, 'high': float,'low': float, 'close': float})
        df = df.iloc[:-1].reset_index(drop=True)
        if len(df) < EMA_PERIOD + 50: return None
        return df
    except Exception as e: print(f"Binance Kline Error {symbol}: {e}", flush=True)
    return None

def get_klines(symbol, interval='5'):
    df = get_klines_bybit(symbol, interval=interval)
    if df is not None: print(f"Bot2: [{symbol}] Data from Bybit", flush=True); return df
    df = get_klines_binance(symbol, interval=f"{interval}m")
    if df is not None: print(f"Bot2: [{symbol}] Data from Binance", flush=True); return df
    df = get_klines_coindcx(symbol, interval=f"{interval}m")
    if df is not None: print(f"Bot2: [{symbol}] Data from CoinDCX", flush=True); return df
    print(f"Bot2: [{symbol}] Data nahi mila kahi se bhi", flush=True)
    return None

def check_paper_trades(df, symbol):
    global total_pnl_lifetime
    if symbol not in PAPER_TRADES: return
    trade = PAPER_TRADES[symbol]
    if trade['status']!= 'OPEN': return
    current_price = df['close'].iloc[-1]
    tp = trade['tp']
    sl = trade['sl']
    cdcx_name = symbol.replace('USDT', '-USDT')
    entry_time = trade['time']
    if current_price <= tp:
        pnl = ((trade['entry'] - tp) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_TP'; trade['exit'] = tp; trade['pnl'] = round(pnl, 2); trade['exit_time'] = time.time()
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"✅ <b>TRADE CLOSED - TARGET HIT</b> ✅\n\n<b>Coin:</b> {cdcx_name}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit TP:</b> ${tp:.6f}\n<b>PnL:</b> +{pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%\n<b>Duration:</b> {duration} min"
        send_telegram(msg); send_ntfy_plain(msg)
        print(f"Paper Trade TP: {cdcx_name} +{pnl:.2f}% in {duration}min", flush=True)
    elif current_price >= sl:
        pnl = ((trade['entry'] - sl) / trade['entry']) * 100
        duration = int((time.time() - entry_time) / 60)
        trade['status'] = 'CLOSED_SL'; trade['exit'] = sl; trade['pnl'] = round(pnl, 2); trade['exit_time'] = time.time()
        save_total_pnl(total_pnl_lifetime + pnl)
        msg = f"❌ <b>TRADE CLOSED - SL HIT</b> ❌\n\n<b>Coin:</b> {cdcx_name}\n<b>Entry:</b> ${trade['entry']:.6f}\n<b>Exit SL:</b> ${sl:.6f}\n<b>PnL:</b> {pnl:.2f}%\n<b>Lifetime PnL:</b> {total_pnl_lifetime:.2f}%\n<b>Duration:</b> {duration} min"
        send_telegram(msg); send_ntfy_plain(msg)
        print(f"Paper Trade SL: {cdcx_name} {pnl:.2f}% in {duration}min", flush=True)
    save_paper_trades()

def bot1_scan_bybit_futures():
    load_watchlist()
    print("Bot1 started — Triple Source (Bybit + CoinDCX + Bybit Pump Scan)", flush=True)
    while True:
        alerted_symbols = set()
        try:
            coindcx_futures = get_coindcx_futures_symbols()
            if not coindcx_futures: print("Bot1: CoinDCX futures list nahi mili, 60s wait...", flush=True); time.sleep(60); continue
            try:
                url = "https://api.bybit.com/v5/market/tickers"
                params = {'category': 'linear'}
                data = requests.get(url, params=params, timeout=20).json()
                if data['retCode'] == 0:
                    tickers = data['result']['list']; pumped_bybit = 0
                    for ticker in tickers:
                        symbol = ticker['symbol']
                        if not symbol.endswith('USDT'): continue
                        change_24h = float(ticker['price24hPcnt']) * 100
                        if change_24h >= PUMP_PERCENT_24H: process_pump_alert(symbol, change_24h, ticker['lastPrice'], 'Bybit Futures', alerted_symbols); pumped_bybit += 1
                    print(f"Bot1 [Bybit Pump]: {len(tickers)} pairs checked | Pumped: {pumped_bybit}", flush=True)
                else: print(f"Bot1 Bybit API Error: {data['retMsg']}", flush=True)
            except Exception as e: print(f"Bot1 Bybit Pump Error: {e}", flush=True)
            bybit_symbols_found = set()
            try:
                url = "https://api.bybit.com/v5/market/tickers"
                params = {'category': 'linear'}
                headers = {'User-Agent': 'Mozilla/5.0'}
                response = requests.get(url, params=params, headers=headers, timeout=20)
                data = response.json()
                if data['retCode'] == 0:
                    tickers = data['result']['list']; cdcx_count = 0; pumped = 0
                    for ticker in tickers:
                        symbol = ticker['symbol']
                        if symbol not in coindcx_futures: continue
                        cdcx_count += 1; bybit_symbols_found.add(symbol)
                        change_24h = float(ticker['price24hPcnt']) * 100
                        if change_24h >= PUMP_PERCENT_24H: process_pump_alert(symbol, change_24h, ticker['lastPrice'], 'Bybit Futures', alerted_symbols); pumped += 1
                    print(f"Bot1 [Bybit Match]: {cdcx_count} pairs checked | Pumped: {pumped}", flush=True)
                else: print(f"Bot1 Bybit API Error: {data['retMsg']}", flush=True)
            except Exception as e: print(f"Bot1 Bybit Error: {e}", flush=True)
            try:
                coindcx_only = coindcx_futures - bybit_symbols_found
                url = "https://api.coindcx.com/exchange/ticker"
                headers = {'User-Agent': 'Mozilla/5.0'}
                res = requests.get(url, headers=headers, timeout=20).json()
                cdcx_map = {}
                for t in res:
                    market = t.get('market', '')
                    if market.endswith('USDT'):
                        base = market.replace('_USDT', '').replace('USDT', '')
                        symbol = f"{base}USDT"; cdcx_map[symbol] = t
                pumped_cdcx = 0
                for symbol in coindcx_only:
                    if symbol not in cdcx_map: continue
                    ticker = cdcx_map[symbol]
                    try:
                        change_str = str(ticker.get('change_24_hour', ticker.get('change_24h', '0')))
                        change_24h = float(change_str); price = ticker.get('last_price', '0')
                        if change_24h >= PUMP_PERCENT_24H: process_pump_alert(symbol, change_24h, price, 'CoinDCX Futures', alerted_symbols); pumped_cdcx += 1
                    except Exception as e: continue
                print(f"Bot1 [CoinDCX]: {len(coindcx_only)} pairs checked | Pumped: {pumped_cdcx}", flush=True)
            except Exception as e: print(f"Bot1 CoinDCX Error: {e}", flush=True)
            print(f"Bot1: Total Watchlist: {len(WATCHLIST)} coins\n", flush=True)
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        time.sleep(300)

def bot2_supertrend_short():
    print("Bot2 started", flush=True)
    while True:
        global WATCHLIST
        if isinstance(WATCHLIST, list):
            temp_list = WATCHLIST.copy(); WATCHLIST = {}
            for symbol in temp_list: WATCHLIST[symbol] = {'time': time.time(), 'cross_count': 0, 'last_state': 'not_short'}
        try:
            if not WATCHLIST: print("Bot2: Watchlist empty, 30s wait...", flush=True); time.sleep(30); continue
            print(f"\nBot2: ===== NEW CYCLE — {len(WATCHLIST)} coins =====", flush=True)
            to_remove = []
            for symbol, info in list(WATCHLIST.items()):
                cdcx_name = symbol.replace('USDT', '-USDT')
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400: print(f"Bot2: [{cdcx_name}] Expire — remove", flush=True); to_remove.append(symbol); continue
                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD + 2:
                    print(f"Bot2: [{cdcx_name}] SKIP — data nahi mila", flush=True)
                    if info.get('data_fail_count', 0) >= 2: to_remove.append(symbol); print(f"Bot2: [{cdcx_name}] 3x fail — watchlist se remove", flush=True)
                    else: WATCHLIST[symbol]['data_fail_count'] = info.get('data_fail_count', 0) + 1
                    continue
                else: WATCHLIST[symbol]['data_fail_count'] = 0
                try: df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                except Exception as e: print(f"Bot2: [{cdcx_name}] ST error: {e}", flush=True); continue
                st_line = df['st_line'].iloc[-1]; ema_val = df['ema_val'].iloc[-1]; close_price = df['close'].iloc[-1]
                if any(math.isnan(v) for v in [st_line, ema_val, close_price]): print(f"Bot2: [{cdcx_name}] SKIP — NaN", flush=True); continue
                check_paper_trades(df, symbol)
                price_below_st = close_price < st_line; st_below_ema = st_line < ema_val; current_short = price_below_st and st_below_ema
                reset_state = (close_price > st_line) and (st_line > ema_val)
                last_state = info.get('last_state', 'reset'); new_cross = (last_state == 'reset' and current_short == True)
                print(f"Bot2: [{cdcx_name}] Price={close_price:.6f} | ST={st_line:.6f} | EMA{EMA_PERIOD}={ema_val:.6f} | SHORT={current_short} | NEW_CROSS={new_cross}", flush=True)
                if new_cross:
                    cross_count = info.get('cross_count', 0)
                    if cross_count >= 3: print(f"Bot2: [{cdcx_name}] Limit 3/3 reach — skip", flush=True)
                    else:
                        tp_price = round(close_price * 0.95, 6); sl_price = round(close_price * 1.02, 6)
                        PAPER_TRADES[symbol] = {'entry': close_price, 'tp': tp_price, 'sl': sl_price, 'status': 'OPEN', 'time': time.time()}
                        save_paper_trades()
                        msg = f"📝 <b>PAPER SHORT ENTRY</b> 📝\n\n<b>Coin:</b> {cdcx_name}\n<b>Signal:</b> #{cross_count + 1}/3\n<b>Entry:</b> ${close_price:.6f}\n<b>TP 5%:</b> ${tp_price}\n<b>SL 2%:</b> ${sl_price}\n\nPrice &lt; ST &lt; EMA{EMA_PERIOD}"
                        send_telegram(msg); send_ntfy_plain(msg)
                        WATCHLIST[symbol]['cross_count'] = cross_count + 1
                        print(f"Bot2: [{cdcx_name}] ✅ PAPER SHORT #{cross_count + 1} @ {close_price:.6f}", flush=True)
                if current_short: WATCHLIST[symbol]['last_state'] = 'short'
                elif reset_state: WATCHLIST[symbol]['last_state'] = 'reset'
                save_watchlist(); time.sleep(1)
            for symbol in to_remove: WATCHLIST.pop(symbol, None); save_watchlist()
            print(f"Bot2: ===== CYCLE COMPLETE — 30s wait =====\n", flush=True)
        except Exception as e: import traceback; print(f"Bot2 Error: {e}", flush=True); print(traceback.format_exc(), flush=True)
        time.sleep(30)

@app.route('/')
def home():
    open_trades = sum(1 for t in PAPER_TRADES.values() if t['status'] == 'OPEN')
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins. Open Paper Trades: {open_trades}. Lifetime PnL: {total_pnl_lifetime:.2f}%"

@app.route('/watchlist')
def show_watchlist(): return jsonify(WATCHLIST)

@app.route('/papertrades')
def show_papertrades(): return jsonify(PAPER_TRADES)

def run_telegram_bot():
    global telegram_app
    telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("add", add_command))
    telegram_app.add_handler(CommandHandler("remove", remove_command))
    telegram_app.add_handler(CommandHandler("watchlist", watchlist_command))
    print("Telegram Bot commands started", flush=True)
    telegram_app.run_polling()

if __name__ == '__main__':
    print(f"BOT_TOKEN set: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID set: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    print(f"GITHUB_TOKEN set: {bool(GITHUB_TOKEN)}", flush=True)
    print(f"NTFY_TOPIC set: {bool(os.environ.get('NTFY_TOPIC'))}", flush=True)
    time.sleep(5)
    load_watchlist()
    load_paper_trades()
    load_total_pnl()
    threading.Thread(target=run_telegram_bot, daemon=True).start() # Naya thread
    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    threading.Thread(target=bot2_supertrend_short, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)