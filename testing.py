# COINDEX V8.8.2 - NSE ORB FIX + SL 3.5% + NO #3 + MANUAL /nseorb
import threading, asyncio, httpx, time, os, json, pandas as pd, numpy as np, math, logging, traceback, pytz
from decimal import Decimal, ROUND_DOWN
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from datetime import datetime
import yfinance as yf

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

PUMP_PERCENT_24H = 40
TRIGGER_TICKS = 2
TARGET_TP_PERCENT = 0.05
EMERGENCY_SL_PERCENT = 0.035
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
POSITION_SIZE_PERCENT = 0.20
WATCHLIST_DAYS = 2
MAX_OPEN_TRADES = 4
MIN_TURNOVER_24H = 2000000
ENABLE_ATTEMPT_3 = False
TAKER_FEE = 0.0005
GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)
SLIPPAGE_PCT = 0.001
MAX_DISTANCE_PCT = 0.005
WEBHOOK_SECRET = os.environ.get("TELEGRAM_SECRET", "change_this_secret_123")

IST = pytz.timezone('Asia/Kolkata')
STOCKS_NSE = ["MPHASIS","SRF","LAURUSLABS","COFORGE","WHIRLPOOL","PGEL","CYIENT","CHENNPETRO","COLPAL","LODHA","RAMRAT","ASIANPAINT","TATACHEM","TORNTPHARM","DMART","GLAND","BALMLAWRIE","APOLLOHOSP","PIDILITIND","MARICO"]
ORB_LEVELS = {}
NSE_SIGNALS_TODAY = set()

GIST_ID = os.environ.get("GIST_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_BOT_TOKEN = BOT_TOKEN
TELEGRAM_CHAT_ID = CHAT_ID
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")

WATCHLIST = {}; PAPER_TRADES = {}; TICK_CACHE = {}
cooldown_coins = {}
_lock = asyncio.Lock(); _gist_lock = asyncio.Lock()
BALANCE_DATA = {"total_balance": 10000.0, "starting_balance": 10000.0, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
application = None
main_event_loop = None
webhook_queue = asyncio.Queue()

def price_to_tick(price, tick):
    try:
        d_price = Decimal(str(price)); d_tick = Decimal(str(tick))
        quantized = (d_price / d_tick).to_integral_value(rounding=ROUND_DOWN) * d_tick
        return float(quantized)
    except: return round(float(price), 8)

async def gist_get(client, filename):
    if not GIST_URL or not GITHUB_TOKEN: return {}
    try:
        r = await client.get(GIST_URL, headers=GIST_HEADERS, timeout=10.0)
        if r.status_code!=200: return {}
        d=r.json()
        if filename in d.get('files',{}):
            c=d['files'][filename]['content']
            return json.loads(c) if c else {}
    except: return {}
    return {}
async def gist_set_locked(client, filename, content):
    if not GIST_URL or not GITHUB_TOKEN: return False
    async with _gist_lock:
        payload={"files":{filename:{"content": json.dumps(content, indent=2)}}}
        for _ in range(3):
            try:
                r=await client.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=15.0)
                if r.status_code==200: return True
            except: await asyncio.sleep(2)
    return False
async def save_watchlist(c):
    async with _lock: snapshot = dict(WATCHLIST)
    await gist_set_locked(c, 'watchlist.json', {'coins': snapshot})
async def save_paper_trades(c):
    async with _lock: snapshot = dict(PAPER_TRADES)
    await gist_set_locked(c, 'paper_trades.json', snapshot)
async def save_balance_data(c):
    async with _lock: snapshot = dict(BALANCE_DATA)
    await gist_set_locked(c, 'total_pnl.json', snapshot)
async def load_watchlist(c):
    global WATCHLIST
    data=await gist_get(c, 'watchlist.json')
    async with _lock:
        WATCHLIST={}
        if data and 'coins' in data:
            for s,d in data['coins'].items():
                if isinstance(d, dict):
                    cs=s.replace('.P','')
                    WATCHLIST[cs]=d
                    WATCHLIST[cs].setdefault('last_state','reset')
                    WATCHLIST[cs].setdefault('attempts',0)
                    WATCHLIST[cs].setdefault('trigger_low',None)
async def load_paper_trades(c):
    global PAPER_TRADES
    data = await gist_get(c, 'paper_trades.json') or {}
    async with _lock: PAPER_TRADES = data
async def load_balance_data(c):
    global BALANCE_DATA
    d=await gist_get(c, 'total_pnl.json')
    if d and 'total_balance' in d:
        async with _lock: BALANCE_DATA=d
async def send_telegram(client, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try: await client.post(url, json=payload, timeout=10.0)
    except: pass

async def start_command(u,c): await u.message.reply_text("✅ Bot v8.8.2 NSE FIX - 3.5% SL + NO #3 + /nseorb", parse_mode="HTML")
async def add_command(u,c):
    if c.args:
        s=c.args[0].upper().replace('.P','')
        async with _lock: WATCHLIST[s]={'time':time.time(),'attempts':0,'last_state':'reset','trigger_low':None}
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"✅ {s} added", parse_mode="HTML")
async def remove_command(u,c):
    if c.args:
        s=c.args[0].upper().replace('.P','')
        async with _lock: WATCHLIST.pop(s,None); cooldown_coins.pop(s,None)
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"🗑️ {s} removed", parse_mode="HTML")
async def watchlist_command(u,c):
    msg=""
    async with _lock:
        for s,d in WATCHLIST.items():
            trig=d.get('trigger_low'); t_str = f"${trig:.8f}" if trig else "None"
            msg+=f"{s} #{d.get('attempts',0)+1} {d.get('last_state')} L:{t_str}\n"
        if cooldown_coins:
            msg+="\n⏳ Cooldown:\n"
            for s,ts in cooldown_coins.items():
                rem=max(0,(ts-time.time())/3600); msg+=f"{s} {rem:.1f}hr\n"
        msg+=f"\n📊 NSE ORB: {len(ORB_LEVELS)} stocks\n"
        if ORB_LEVELS:
            for k,v in list(ORB_LEVELS.items())[:5]: msg+=f"{k} H:{v['high']:.1f} L:{v['low']:.1f}\n"
        else:
            msg+=f"Empty - /nseorb se banao\n"
    if not msg: msg="Empty"
    await u.message.reply_text(f"📋 Watchlist ({len(WATCHLIST)}):\n{msg}", parse_mode="HTML")
async def open_command(u,c):
    async with _lock: o={k:v for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'}
    if not o: return await u.message.reply_text("No Open Trades", parse_mode="HTML")
    msg=f"📊 OPEN ({len(o)}/{MAX_OPEN_TRADES})\n\n"
    for s,t in o.items(): msg+=f"{s} #{t.get('attempt',1)}/3 Entry ${t['entry']:.8f}\n"
    await u.message.reply_text(msg, parse_mode="HTML")
async def pnl_command(u,c):
    async with _lock: b=dict(BALANCE_DATA)
    await u.message.reply_text(f"Balance: ${b['total_balance']:.2f} PnL: {b['lifetime_pnl_percent']:.2f}% (${b['lifetime_pnl_usdt']:.2f})", parse_mode="HTML")
async def close_command(u,c):
    if not c.args: return await u.message.reply_text("Use /close SYMBOL")
    s=c.args[0].upper().replace('.P','')
    cl=c.bot_data.get("http_client")
    async with _lock:
        if s not in PAPER_TRADES or PAPER_TRADES[s]['status']!='OPEN': return await u.message.reply_text("No open trade")
        tr=PAPER_TRADES[s].copy()
    df=await get_klines(cl, s, include_current=True)
    if df is None: return await u.message.reply_text("Fetch failed")
    ep=df['close'].iloc[-1]
    async with _lock:
        amt_orig = tr.get('trade_amount_usdt', tr['balance_at_entry']*POSITION_SIZE_PERCENT)
        ratio = 0.5 if tr.get('tp1_hit') else 1.0; amt = amt_orig * ratio
        gpct=((tr['entry']-ep)/tr['entry'])*100; gusdt=amt*gpct/100
        fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
        BALANCE_DATA['total_balance']+=nusdt
        BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
        BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
        PAPER_TRADES[s]['status']='CLOSED_MANUAL'; PAPER_TRADES[s]['pnl_percent']=round((nusdt/amt)*100,2) if amt>0 else 0; PAPER_TRADES[s]['pnl_usdt']=round(nusdt,2); WATCHLIST.pop(s,None)
    if cl: await save_paper_trades(cl); await save_balance_data(cl); await save_watchlist(cl)
    await u.message.reply_text(f"Closed {s} PnL ${nusdt:.2f}", parse_mode="HTML")
async def exit_command(u,c):
    if not c.args: return await u.message.reply_text("Use: <code>/exit SYMBOL</code> ya <code>/exit all</code>", parse_mode="HTML")
    sym_arg = c.args[0].upper().replace('.P','')
    if sym_arg == "ALL": return await exitall_command(u,c)
    c.args = [sym_arg]; await close_command(u,c)
async def exitall_command(u,c):
    cl = c.bot_data.get("http_client")
    async with _lock: open_syms = [k for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN']
    if not open_syms: return await u.message.reply_text("No Open Trades", parse_mode="HTML")
    total_pnl = 0; closed_count = 0
    for s in open_syms:
        try:
            async with _lock: tr = PAPER_TRADES.get(s,{}).copy()
            if tr.get('status')!='OPEN': continue
            df = await get_klines(cl, s, include_current=True)
            if df is None: continue
            ep = df['close'].iloc[-1]
            async with _lock:
                amt_orig = tr.get('trade_amount_usdt', tr['balance_at_entry']*POSITION_SIZE_PERCENT)
                ratio = 0.5 if tr.get('tp1_hit') else 1.0; amt = amt_orig * ratio
                gpct = ((tr['entry']-ep)/tr['entry'])*100; gusdt = amt*gpct/100
                fee = amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt = gusdt-fee
                BALANCE_DATA['total_balance']+=nusdt
                BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
                BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
                PAPER_TRADES[s]['status']='CLOSED_EXITALL'; PAPER_TRADES[s]['pnl_percent']=round((nusdt/amt)*100,2) if amt>0 else 0; PAPER_TRADES[s]['pnl_usdt']=round(nusdt,2); WATCHLIST.pop(s,None); total_pnl += nusdt; closed_count += 1
        except: pass
    if cl: await save_paper_trades(cl); await save_balance_data(cl); await save_watchlist(cl)
    await u.message.reply_text(f"💥 EXIT ALL DONE Closed: {closed_count} PnL: ${total_pnl:.2f}", parse_mode="HTML")
async def resetpnl_command(u,c):
    if not c.args or c.args[0].lower()!= "confirm": return await u.message.reply_text("⚠️ Confirm: <code>/resetpnl confirm</code>", parse_mode="HTML")
    async with _lock:
        BALANCE_DATA['total_balance'] = BALANCE_DATA['starting_balance']; BALANCE_DATA['lifetime_pnl_usdt'] = 0.0; BALANCE_DATA['lifetime_pnl_percent'] = 0.0
        PAPER_TRADES.clear(); WATCHLIST.clear(); cooldown_coins.clear(); ORB_LEVELS.clear(); NSE_SIGNALS_TODAY.clear()
    cl = c.bot_data.get("http_client")
    if cl: await save_balance_data(cl); await save_paper_trades(cl); await save_watchlist(cl)
    await u.message.reply_text("✅ PNL RESET DONE Balance $10000", parse_mode="HTML")
async def help_command(u,c):
    await u.message.reply_text("📋 <b>V8.8.2 NSE FIX</b>\nSL 3.5% | No #3 | <code>/watchlist</code> <code>/nseorb</code> <code>/pnl</code>", parse_mode="HTML")
async def nseorb_command(u,c):
    global ORB_LEVELS
    await u.message.reply_text("⏳ NSE ORB manual calc... 20 stocks ~30 sec", parse_mode="HTML")
    ORB_LEVELS.clear()
    count=0; failed=[]
    for sym in STOCKS_NSE:
        for retry in range(3):
            orb = await asyncio.to_thread(get_nse_orb_sync, sym)
            if orb: ORB_LEVELS[sym]=orb; count+=1; break
            await asyncio.sleep(1)
        if sym not in ORB_LEVELS: failed.append(sym)
        await asyncio.sleep(0.3)
    msg = f"📊 <b>NSE ORB READY MANUAL</b> {count}/{len(STOCKS_NSE)}\n"
    if ORB_LEVELS:
        for k,v in list(ORB_LEVELS.items())[:10]: msg+=f"{k} H:{v['high']:.1f} L:{v['low']:.1f}\n"
    if failed: msg+=f"\nFailed: {','.join(failed[:5])}"
    await u.message.reply_text(msg, parse_mode="HTML")
    cl=c.bot_data.get("http_client")
    if cl: await save_watchlist(cl)

async def get_klines_bybit_async(client, symbol, interval='5', limit=1000, include_current=False):
    url="https://api.bybit.com/v5/market/kline"
    by=symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    params={'category':'linear','symbol':by,'interval':interval,'limit':limit}
    try:
        res=await client.get(url, params=params, timeout=10.0)
        data=res.json()
        if data.get('retCode')==0 and data['result']['list']:
            df=pd.DataFrame(data['result']['list'], columns=['timestamp','open','high','low','close','volume','turnover'])
            df=df.astype({'timestamp':'int64','open':float,'high':float,'low':float,'close':float})
            df=df.iloc[::-1].reset_index(drop=True)
            if not include_current: df=df.iloc[:-1].reset_index(drop=True)
            if len(df)>=50: return df
    except: pass
    return None
async def get_tick_size(client, symbol):
    if symbol in TICK_CACHE: return TICK_CACHE[symbol]
    url="https://api.bybit.com/v5/market/instruments-info"
    by=symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    params={'category':'linear','symbol':by}
    try:
        res=await client.get(url, params=params, timeout=10.0)
        data=res.json()
        if data.get('retCode')==0 and data['result']['list']:
            tick=float(data['result']['list'][0]['priceFilter']['tickSize'])
            TICK_CACHE[symbol]=tick; return tick
    except: return None
    return None
async def get_live_price(client, symbol):
    url="https://api.bybit.com/v5/market/tickers"
    by=symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    params={'category':'linear','symbol':by}
    try:
        res=await client.get(url, params=params, timeout=3.0)
        data=res.json()
        if data.get('retCode')==0 and data['result']['list']:
            return float(data['result']['list'][0]['lastPrice'])
    except: pass
    return None
async def get_klines(client, symbol, interval='5', limit=1000, include_current=False):
    return await get_klines_bybit_async(client, symbol, interval, limit, include_current)

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()
    high, low, close = df['high'].values, df['low'].values, df['close'].values
    hl2 = (high + low) / 2.0
    h_l = high - low
    h_pc = np.abs(high - np.roll(close, 1))
    l_pc = np.abs(low - np.roll(close, 1))
    h_pc[0] = h_l[0]; l_pc[0] = h_l[0]
    tr = np.maximum(h_l, np.maximum(h_pc, l_pc))
    atr = np.zeros(len(tr))
    atr[0] = np.mean(tr[:period])
    for i in range(1, len(tr)):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    upperband = hl2 + (multiplier * atr)
    lowerband = hl2 - (multiplier * atr)
    n = len(df)
    final_upperband = np.zeros(n); final_lowerband = np.zeros(n)
    supertrend = np.ones(n, dtype=bool)
    st_line = np.zeros(n); st_dir = np.zeros(n, dtype=int)
    for i in range(n):
        if i == 0:
            final_upperband[i] = upperband[i]; final_lowerband[i] = lowerband[i]
            st_line[i] = upperband[i]; st_dir[i] = 1; supertrend[i] = False
            continue
        if upperband[i] < final_upperband[i-1] or close[i-1] > final_upperband[i-1]: final_upperband[i] = upperband[i]
        else: final_upperband[i] = final_upperband[i-1]
        if lowerband[i] > final_lowerband[i-1] or close[i-1] < final_lowerband[i-1]: final_lowerband[i] = lowerband[i]
        else: final_lowerband[i] = final_lowerband[i-1]
        prev_st = supertrend[i-1]
        if prev_st and close[i] < final_lowerband[i]: supertrend[i] = False
        elif not prev_st and close[i] > final_upperband[i]: supertrend[i] = True
        else: supertrend[i] = prev_st
        if supertrend[i]: st_line[i] = final_lowerband[i]; st_dir[i] = -1
        else: st_line[i] = final_upperband[i]; st_dir[i] = 1
    df['st_line'] = st_line; df['st_dir'] = st_dir; df['ema_val'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean(); df['atr'] = atr
    return df

async def check_paper_trades(client, df_live, df_closed, symbol):
    try:
        async with _lock:
            if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!= 'OPEN': return
            trade = PAPER_TRADES[symbol].copy()
        clow = min(float(df_closed['low'].iloc[-1]), float(df_live['low'].iloc[-1]))
        chigh = max(float(df_closed['high'].iloc[-1]), float(df_live['high'].iloc[-1]))
        entry = trade['entry']; attempt = trade.get('attempt', 1)
        amt_orig = trade.get('trade_amount_usdt', trade['balance_at_entry'] * POSITION_SIZE_PERCENT)
        if chigh >= trade['sl']:
            ratio = 0.5 if (attempt == 1 and trade.get('tp1_hit')) else 1.0; tamt = amt_orig * ratio
            gpct = ((entry - trade['sl']) / entry) * 100; gusdt = tamt * gpct / 100; fee = tamt * EFFECTIVE_FEE_RATE + max(0, tamt + gusdt) * EFFECTIVE_FEE_RATE; nusdt = gusdt - fee
            async with _lock:
                BALANCE_DATA['total_balance'] += nusdt; BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']; BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100
                PAPER_TRADES[symbol]['status'] = 'CLOSED_SL'; PAPER_TRADES[symbol]['pnl_percent'] = round((nusdt / tamt) * 100, 2) if tamt > 0 else 0; PAPER_TRADES[symbol]['pnl_usdt'] = round(nusdt, 2)
                if attempt == 1:
                    if trade.get('tp1_hit'): WATCHLIST.pop(symbol, None); rmsg = f"❌ <b>SL BE HIT</b> {symbol} #{attempt}/3 🗑️"
                    else: WATCHLIST[symbol]['attempts'] = 1; WATCHLIST[symbol]['last_state'] = 'reset'; WATCHLIST[symbol]['trigger_low'] = None; rmsg = f"❌ <b>SL HIT</b> {symbol} #{attempt}/3 ⏳ Next #2 (SL 3.5%)"
                elif attempt == 2:
                    if ENABLE_ATTEMPT_3:
                        WATCHLIST[symbol]['attempts'] = 2; WATCHLIST[symbol]['last_state'] = 'wait_above_st'; WATCHLIST[symbol]['trigger_low'] = None; rmsg = f"❌ <b>SL HIT</b> {symbol} #{attempt}/3 ⏳ Waiting ST above LIVE"
                    else:
                        WATCHLIST.pop(symbol, None); cooldown_coins[symbol] = time.time() + 3*3600; rmsg = f"❌ <b>SL HIT</b> {symbol} #{attempt}/2 🗑️ END (No #3) - 3hr cooldown"
                else: WATCHLIST.pop(symbol, None); cooldown_coins[symbol] = time.time() + 3*3600; rmsg = f"❌ <b>SL HIT</b> {symbol} #{attempt}/3 🗑️ 3hr cooldown"
            await save_balance_data(client); await save_paper_trades(client); await save_watchlist(client); asyncio.create_task(send_telegram(client, rmsg)); return
        if clow <= trade['tp']:
            if attempt == 1 and not trade.get('tp1_hit'):
                partial = amt_orig * 0.5; gpct = ((entry - trade['tp']) / entry) * 100; gusdt = partial * gpct / 100
                fee = partial * EFFECTIVE_FEE_RATE + (partial + gusdt) * EFFECTIVE_FEE_RATE; nusdt = gusdt - fee
                async with _lock:
                    if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status'] == 'OPEN':
                        BALANCE_DATA['total_balance'] += nusdt; BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']; BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100
                        PAPER_TRADES[symbol]['tp1_hit'] = True; PAPER_TRADES[symbol]['sl'] = entry; PAPER_TRADES[symbol]['max_favorable_pnl_pct'] = 5.0
                await save_balance_data(client); await save_paper_trades(client)
                asyncio.create_task(send_telegram(client, f"🎯 <b>50% TP1 BOOKED -5%</b> {symbol} #{attempt}/3 SL->BE")); return
            else:
                amt = amt_orig * (0.5 if trade.get('tp1_hit') else 1.0)
                gpct = ((entry - trade['tp']) / entry) * 100; gusdt = amt * gpct / 100; fee = amt * EFFECTIVE_FEE_RATE + (amt + gusdt) * EFFECTIVE_FEE_RATE; nusdt = gusdt - fee; npct = (nusdt / amt) * 100 if amt > 0 else 0
                async with _lock:
                    BALANCE_DATA['total_balance'] += nusdt; BALANCE_DATA['lifetime_pnl_usdt'] = BALANCE_DATA['total_balance'] - BALANCE_DATA['starting_balance']; BALANCE_DATA['lifetime_pnl_percent'] = (BALANCE_DATA['lifetime_pnl_usdt'] / BALANCE_DATA['starting_balance']) * 100
                    PAPER_TRADES[symbol]['status'] = 'CLOSED_TP'; PAPER_TRADES[symbol]['pnl_percent'] = round(npct, 2); PAPER_TRADES[symbol]['pnl_usdt'] = round(nusdt, 2); WATCHLIST.pop(symbol, None)
                await save_balance_data(client); await save_paper_trades(client); await save_watchlist(client)
                asyncio.create_task(send_telegram(client, f"✅ <b>100% TP HIT #{attempt}</b> {symbol}")); return
        if trade.get('tp1_hit') and attempt == 1:
            max_drop = ((entry - clow) / entry) * 100; prev_max = trade.get('max_favorable_pnl_pct', 5.0)
            if max_drop > prev_max:
                steps = math.floor(max_drop - 5.0)
                if steps > math.floor(prev_max - 5.0):
                    locked = steps * 1.0; new_sl = entry * (1 - locked / 100.0); new_sl = price_to_tick(new_sl, await get_tick_size(client, symbol) or 0.0001)
                    if new_sl < trade['sl']:
                        async with _lock:
                            if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status'] == 'OPEN':
                                PAPER_TRADES[symbol]['sl'] = new_sl; PAPER_TRADES[symbol]['max_favorable_pnl_pct'] = max_drop
                        await save_paper_trades(client)
    except Exception as e: print(f"check trades error {symbol}: {e}", flush=True)

async def bot1_scan(client):
    print("Bot1: Started v8.8.2", flush=True)
    while True:
        try:
            url="https://api.bybit.com/v5/market/tickers?category=linear"
            res=await client.get(url, timeout=20.0); data=res.json(); added=0
            if data.get('retCode')==0 and data.get('result'):
                for t in data['result']['list']:
                    m=t.get('symbol','')
                    if not m.endswith('USDT'): continue
                    s=m.replace('.P','')
                    try: ch=float(t.get('price24hPcnt',0))*100; turnover=float(t.get('turnover24h',0))
                    except: continue
                    if turnover < MIN_TURNOVER_24H: continue
                    async with _lock:
                        if ch >= PUMP_PERCENT_24H and s not in WATCHLIST:
                            if s in cooldown_coins:
                                if time.time() < cooldown_coins[s]: continue
                                else: del cooldown_coins[s]
                            WATCHLIST[s]={'time':time.time(),'attempts':0,'last_state':'reset','trigger_low':None}; added+=1
                            asyncio.create_task(send_telegram(client, f"🚨 <b>40%+ PUMP</b> {s} +{ch:.2f}%"))
            if added>0: await save_watchlist(client)
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(60)

async def process_symbol(client, symbol):
    try:
        df_live_raw=await get_klines(client, symbol, include_current=True, limit=1000)
        if df_live_raw is None or len(df_live_raw) < EMA_PERIOD+2: return False
        df_closed_raw=df_live_raw.iloc[:-1].reset_index(drop=True)
        df_live=await asyncio.to_thread(calculate_supertrend, df_live_raw, ATR_PERIOD, ATR_MULTIPLIER)
        df_closed=await asyncio.to_thread(calculate_supertrend, df_closed_raw, ATR_PERIOD, ATR_MULTIPLIER)
        await check_paper_trades(client, df_live, df_closed, symbol)
        low_live=float(df_live['low'].iloc[-1]); close_closed=float(df_closed['close'].iloc[-1]); prev_close_closed=float(df_closed['close'].iloc[-2])
        ema_closed=float(df_closed['ema_val'].iloc[-1]); prev_ema_closed=float(df_closed['ema_val'].iloc[-2])
        st_closed=float(df_closed['st_line'].iloc[-1]); prev_st_closed=float(df_closed['st_line'].iloc[-2]); st_dir_closed=int(df_closed['st_dir'].iloc[-1]); low_closed=float(df_closed['low'].iloc[-1])
        st_live=float(df_live['st_line'].iloc[-1]); st_dir_live=int(df_live['st_dir'].iloc[-1])
        changed=False; new=False; msg=None
        tick=await get_tick_size(client, symbol)
        if tick is None: return False
        raw_live = await get_live_price(client, symbol)
        if raw_live is None: return False
        live_price_for_check = float(raw_live)
        async with _lock:
            if symbol not in WATCHLIST: return False
            pt = PAPER_TRADES.get(symbol); open_exists = pt and pt.get('status') == 'OPEN'
            if open_exists: return False
            att = WATCHLIST[symbol].get('attempts', 0)
            if att >= 2 and not ENABLE_ATTEMPT_3:
                if WATCHLIST[symbol].get('last_state') == 'wait_above_st':
                    WATCHLIST.pop(symbol, None)
                    cooldown_coins[symbol] = time.time() + 3*3600
                    changed = True
                    asyncio.create_task(send_telegram(client, f"🛑 <b>SKIP #3</b> {symbol} - #3 disabled, 3hr cooldown"))
                return changed
            active = sum(1 for t in PAPER_TRADES.values() if t.get('status') == 'OPEN')
            should = False; exec_price = 0.0; trig_for_msg = 0.0
            if att == 0:
                trig = WATCHLIST[symbol].get('trigger_low')
                if trig is None:
                    is_cross = prev_close_closed >= prev_ema_closed and close_closed < ema_closed
                    if is_cross:
                        WATCHLIST[symbol]['trigger_low'] = low_closed; WATCHLIST[symbol]['last_state'] = 'waiting_break_1'; changed = True
                        asyncio.create_task(send_telegram(client, f"📌 <b>1st Trigger Marked</b> {symbol} Low ${low_closed:.8f}"))
                else:
                    if low_live <= trig - (tick * TRIGGER_TICKS):
                        should = True; trig_for_msg = trig; exec_price = (trig - (tick * TRIGGER_TICKS)) * (1 - SLIPPAGE_PCT)
                        if abs(live_price_for_check - exec_price) / exec_price > MAX_DISTANCE_PCT: should = False
            elif att == 1:
                if WATCHLIST[symbol].get('trigger_low') is None:
                    is_cross = st_closed < ema_closed and prev_st_closed >= prev_ema_closed and st_dir_closed == 1
                    if is_cross:
                        WATCHLIST[symbol]['trigger_low'] = low_closed; WATCHLIST[symbol]['last_state'] = 'waiting_break_2'; changed = True
                        asyncio.create_task(send_telegram(client, f"📌 <b>2nd Trigger Marked</b> {symbol} Low ${low_closed:.8f}"))
                else:
                    trig = WATCHLIST[symbol]['trigger_low']
                    if low_live <= trig - (tick * TRIGGER_TICKS): should = True; trig_for_msg = trig; exec_price = (trig - (tick * TRIGGER_TICKS)) * (1 - SLIPPAGE_PCT)
            elif att == 2:
                if not ENABLE_ATTEMPT_3:
                    WATCHLIST.pop(symbol, None); cooldown_coins[symbol] = time.time() + 3*3600; changed = True; return changed
                state = WATCHLIST[symbol].get('last_state', 'wait_above_st')
                if state == 'wait_above_st' and live_price_for_check > st_live and st_dir_live == -1:
                    WATCHLIST[symbol]['last_state'] = 'ready_for_st_cross'; WATCHLIST[symbol]['trigger_low'] = None; changed = True
                    asyncio.create_task(send_telegram(client, f"📈 <b>Above ST LIVE</b> {symbol} Live ${live_price_for_check:.8f} > ST ${st_live:.8f} - Ready for #3"))
                elif state == 'ready_for_st_cross' and WATCHLIST[symbol].get('trigger_low') is None:
                    is_cross = close_closed < st_closed and prev_close_closed >= prev_st_closed and st_dir_closed == 1
                    if is_cross: WATCHLIST[symbol]['trigger_low'] = low_closed; WATCHLIST[symbol]['last_state'] = 'waiting_break_3'; changed = True; asyncio.create_task(send_telegram(client, f"📌 <b>3rd Trigger Marked</b> {symbol} Low ${low_closed:.8f}"))
                elif WATCHLIST[symbol].get('last_state') == 'waiting_break_3' and WATCHLIST[symbol].get('trigger_low') is not None:
                    trig = WATCHLIST[symbol]['trigger_low']
                    if low_live <= trig - (tick * TRIGGER_TICKS): should = True; trig_for_msg = trig; exec_price = (trig - (tick * TRIGGER_TICKS)) * (1 - SLIPPAGE_PCT)
            if should and att < (3 if ENABLE_ATTEMPT_3 else 2) and active < MAX_OPEN_TRADES and exec_price > 0:
                ep = price_to_tick(exec_price, tick); tp = price_to_tick(ep * (1 - TARGET_TP_PERCENT), tick); sl = price_to_tick(ep * (1 + EMERGENCY_SL_PERCENT), tick)
                tamt = BALANCE_DATA['total_balance'] * POSITION_SIZE_PERCENT; cur = att + 1
                WATCHLIST[symbol]['attempts'] = cur; WATCHLIST[symbol]['last_state'] = 'short'; WATCHLIST[symbol]['trigger_low'] = None
                PAPER_TRADES[symbol] = {'entry': ep, 'tp': tp, 'sl': sl, 'status': 'OPEN','time': time.time(), 'balance_at_entry': BALANCE_DATA['total_balance'],'trade_amount_usdt': tamt, 'attempt': cur,'max_favorable_pnl_pct': 0.0, 'tp1_hit': False}
                msg = f"⚡ <b>SHORT #{cur} LIVE</b> {symbol} #{cur}/{'3' if ENABLE_ATTEMPT_3 else '2'}\nEntry ${ep:.8f} (Low {trig_for_msg:.8f} - {TRIGGER_TICKS} ticks)\nTP ${tp:.8f} SL ${sl:.8f} (3.5%)"; new = True; changed = True
            if time.time()-WATCHLIST[symbol]['time'] > WATCHLIST_DAYS*86400 and not (PAPER_TRADES.get(symbol,{}).get('status')=='OPEN'): WATCHLIST.pop(symbol,None); changed=True
        if new: await save_paper_trades(client); asyncio.create_task(send_telegram(client, msg))
        return changed
    except Exception as e: print(f"process_symbol error {symbol}: {e}", flush=True); return False

async def bot2_scan(client):
    print("Bot2: Started v8.8.2", flush=True)
    while True:
        try:
            async with _lock: syms=list(WATCHLIST.keys())
            if not syms: await asyncio.sleep(10); continue
            results=await asyncio.gather(*[process_symbol(client, s) for s in syms])
            if any(results): await save_watchlist(client)
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(5)

def get_nse_orb_sync(symbol):
    try:
        df = yf.download(symbol+".NS", period="1d", interval="5m", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty: return None
        df = df.between_time("09:15","09:29")
        if df.empty or len(df)<2: return None
        return {"high": float(df['High'].max()), "low": float(df['Low'].min())}
    except Exception as e:
        print(f"NSE ORB fetch fail {symbol}: {e}", flush=True)
        return None
def get_nse_last_sync(symbol):
    try:
        df = yf.download(symbol+".NS", period="1d", interval="1m", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if df.empty: return None, None
        last = df.iloc[-1]
        tp = (df['High']+df['Low']+df['Close'])/3
        vwap = float((tp*df['Volume']).cumsum().iloc[-1] / df['Volume'].cumsum().iloc[-1])
        return float(last['Close']), vwap
    except: return None, None

async def bot3_nse_orb_async(client):
    print("Bot3 NSE ORB: Started v8.8.2 FIX", flush=True)
    global ORB_LEVELS, NSE_SIGNALS_TODAY
    while True:
        try:
            now_ist = datetime.now(IST)
            if now_ist.weekday() < 5:
                # FIX: 9:15 se 10:00 tak har 10 min try jab tak empty hai
                if not ORB_LEVELS and 9 <= now_ist.hour <= 10:
                    if now_ist.minute % 10 == 1 or now_ist.minute % 10 == 2:
                        print(f"Bot3: Retrying ORB at {now_ist} - empty {len(ORB_LEVELS)}", flush=True)
                        temp={}
                        for sym in STOCKS_NSE:
                            for r in range(3):
                                orb = await asyncio.to_thread(get_nse_orb_sync, sym)
                                if orb: temp[sym]=orb; break
                                await asyncio.sleep(1)
                            await asyncio.sleep(0.3)
                        if temp:
                            ORB_LEVELS.update(temp)
                            await send_telegram(client, f"📊 <b>NSE ORB READY v8.8.2 FIX</b> {len(ORB_LEVELS)}/{len(STOCKS_NSE)} stocks\nUse /nseorb to recalc")
                            print(f"Bot3 ORB Ready: {len(ORB_LEVELS)}", flush=True)
                if ORB_LEVELS:
                    is_market_window = (now_ist.hour == 9 and now_ist.minute >= 31) or (now_ist.hour == 10) or (now_ist.hour == 11 and now_ist.minute <= 5)
                    if is_market_window:
                        for sym, lv in list(ORB_LEVELS.items()):
                            if sym in NSE_SIGNALS_TODAY: continue
                            close_price, vwap = await asyncio.to_thread(get_nse_last_sync, sym)
                            if close_price is None or vwap is None: continue
                            if close_price > lv['high'] and close_price > vwap:
                                NSE_SIGNALS_TODAY.add(sym)
                                await send_telegram(client, f"🚀 <b>NSE LONG</b> {sym}\nPrice: {close_price:.2f}\nORB High: {lv['high']:.2f} Break\nVWAP: {vwap:.2f}\nSL: {lv['low']:.2f}")
                                ORB_LEVELS.pop(sym, None)
                            elif close_price < lv['low'] and close_price < vwap:
                                NSE_SIGNALS_TODAY.add(sym)
                                await send_telegram(client, f"🔻 <b>NSE SHORT</b> {sym}\nPrice: {close_price:.2f}\nORB Low: {lv['low']:.2f} Break\nVWAP: {vwap:.2f}\nSL: {lv['high']:.2f}")
                                ORB_LEVELS.pop(sym, None)
                            await asyncio.sleep(0.5)
                if now_ist.hour == 12 and 10 <= now_ist.minute <= 12 and ORB_LEVELS:
                    await send_telegram(client, f"📊 <b>NSE EOD</b> No more signals. ORB was {len(ORB_LEVELS)} left")
                    ORB_LEVELS.clear()
                if now_ist.hour == 9 and now_ist.minute < 10: NSE_SIGNALS_TODAY.clear()
        except Exception as e: print(f"Bot3 Error: {e}", flush=True)
        await asyncio.sleep(60)

@app.route('/')
def home(): return jsonify({"status":"v8.8.2 NSE FIX","watchlist":len(WATCHLIST),"cooldown":len(cooldown_coins),"nse_orb":len(ORB_LEVELS)})
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if WEBHOOK_SECRET!= "change_this_secret_123":
            secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if secret_header!= WEBHOOK_SECRET: return jsonify({"ok": False, "error": "invalid secret"}), 403
        data = request.get_json(force=True, silent=True)
        if data and main_event_loop: main_event_loop.call_soon_threadsafe(webhook_queue.put_nowait, data)
    except Exception as e: print(f"Webhook parse error: {e}", flush=True)
    return jsonify({"ok": True}), 200
async def process_webhook_queue():
    while True:
        data = await webhook_queue.get()
        try:
            if application and application.bot:
                update = Update.de_json(data, application.bot)
                await application.process_update(update)
        except Exception as e: print(f"Webhook update error: {e}", flush=True)
        finally: webhook_queue.task_done()
async def main_async():
    global application, main_event_loop
    main_event_loop = asyncio.get_running_loop()
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    async with httpx.AsyncClient(limits=limits) as client:
        await load_watchlist(client); await load_paper_trades(client); await load_balance_data(client)
        print(f"Gist Loaded: {len(WATCHLIST)} | Balance: ${BALANCE_DATA['total_balance']:.2f}", flush=True)
        t_req = HTTPXRequest(connection_pool_size=20, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
        app_t = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
        app_t.bot_data["http_client"] = client
        application = app_t
        for cmd, fn in [("start", start_command), ("add", add_command), ("remove", remove_command), ("watchlist", watchlist_command), ("open", open_command), ("close", close_command), ("pnl", pnl_command), ("exit", exit_command), ("exitall", exitall_command), ("resetpnl", resetpnl_command), ("reset", resetpnl_command), ("help", help_command), ("nseorb", nseorb_command)]:
            app_t.add_handler(CommandHandler(cmd, fn))
        await app_t.initialize(); await app_t.start()
        asyncio.create_task(process_webhook_queue())
        if WEBHOOK_URL:
            wh_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            try:
                await app_t.bot.delete_webhook(drop_pending_updates=True); await asyncio.sleep(1)
                await app_t.bot.set_webhook(url=wh_url, drop_pending_updates=True, secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET!= "change_this_secret_123" else None)
                print(f"✅ WEBHOOK SET: {wh_url}", flush=True)
            except Exception as e: print(f"Webhook set error: {e}", flush=True)
        else:
            try: await app_t.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            await asyncio.sleep(5)
            await app_t.updater.start_polling(drop_pending_updates=True, poll_interval=2.0, bootstrap_retries=-1)
        port = int(os.environ.get("PORT", 10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
        asyncio.create_task(bot1_scan(client)); asyncio.create_task(bot2_scan(client)); asyncio.create_task(bot3_nse_orb_async(client))
        print("v8.8.2 NSE FIX Operational", flush=True)
        try:
            while True: await asyncio.sleep(3600)
        except (KeyboardInterrupt, SystemExit):
            try: await app_t.updater.stop()
            except: pass
            await app_t.stop(); await app_t.shutdown()
def main():
    loop=asyncio.get_event_loop()
    loop.run_until_complete(main_async())
if __name__=='__main__': main()