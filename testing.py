# COINDEX V8.8.11 - BOT1/BOT2 FIXED 4 CORRECTIONS + BOT3 SAME V8.8.9 - HTML FIX
import threading, asyncio, httpx, time, os, json, pandas as pd, numpy as np, math, logging, traceback, pytz, functools
from decimal import Decimal, ROUND_DOWN
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from datetime import datetime, timezone

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

PUMP_PERCENT_24H = 40
TRIGGER_TICKS = 2
TARGET_TP_PERCENT = 0.10
EMERGENCY_SL_PERCENT = 0.05
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
POSITION_SIZE_PERCENT = 0.20
WATCHLIST_DAYS = 2
MAX_OPEN_TRADES = 4
MIN_TURNOVER_24H = 10000000
ENABLE_ATTEMPT_3 = True
BOT1_SCAN_INTERVAL = 30
BOT12_STARTING_BALANCE = 10000.0
BOT3_STARTING_BALANCE = 100000.0
NSE_POSITION_SIZE_PERCENT = 0.20
NSE_TARGET_TP_PERCENT = 0.01
NSE_SL_PERCENT = 0.01
NSE_MAX_OPEN_TRADES = 4
NSE_EOD_HOUR = 15
NSE_EOD_MINUTE = 0
NSE_EOD_WINDOW_MINUTES = 10
TAKER_FEE = 0.0005
GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)
SLIPPAGE_PCT = 0.001
MAX_DISTANCE_PCT = 0.005
WEBHOOK_SECRET = os.environ.get("TELEGRAM_SECRET", "change_this_secret_123")
IST = pytz.timezone('Asia/Kolkata')
STOCKS_NSE = ["CHENNPETRO","LODHA","MPHASIS","LAURUSLABS","COFORGE","APOLLOHOSP","PIDILITIND"]
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
NSE_PAPER_TRADES = {}
cooldown_coins = {}
_lock = asyncio.Lock(); _gist_lock = asyncio.Lock()
BOT12_BALANCE_DATA = {"total_balance": BOT12_STARTING_BALANCE, "starting_balance": BOT12_STARTING_BALANCE, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
BOT3_BALANCE_DATA = {"total_balance": BOT3_STARTING_BALANCE, "starting_balance": BOT3_STARTING_BALANCE, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
BALANCE_DATA = BOT12_BALANCE_DATA
application = None
main_event_loop = None
webhook_queue = asyncio.Queue()
BOT1_LAST_SCAN = 0
BOT2_LAST_SCAN = 0

def price_to_tick(price, tick):
    try:
        if not price or not tick or tick <= 0 or price <= 0: return float(price) if price else 0.0
        d_price = Decimal(str(price)); d_tick = Decimal(str(tick))
        if d_tick == 0: return float(price)
        quantized = (d_price / d_tick).to_integral_value(rounding=ROUND_DOWN) * d_tick
        return float(quantized)
    except: return round(float(price), 8) if price else 0.0

def authorized_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not TELEGRAM_CHAT_ID:
            return await func(update, context, *args, **kwargs)
        user_id = str(update.effective_user.id); chat_id = str(update.effective_chat.id); allowed_id = str(TELEGRAM_CHAT_ID)
        if user_id!= allowed_id and chat_id!= allowed_id: return
        return await func(update, context, *args, **kwargs)
    return wrapper

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
    await gist_set_locked(c, 'paper_trades_crypto.json', snapshot)
async def save_bot12_balance(c):
    async with _lock: snapshot = dict(BOT12_BALANCE_DATA)
    await gist_set_locked(c, 'bot12_pnl.json', snapshot)
async def save_bot3_balance(c):
    async with _lock: snapshot = dict(BOT3_BALANCE_DATA)
    await gist_set_locked(c, 'bot3_pnl.json', snapshot)
async def save_nse_paper_trades(c):
    async with _lock: snapshot = dict(NSE_PAPER_TRADES)
    await gist_set_locked(c, 'bot3_trades.json', snapshot)
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
    data = await gist_get(c, 'paper_trades_crypto.json') or await gist_get(c, 'paper_trades.json') or {}
    async with _lock: PAPER_TRADES = data
async def load_bot12_balance(c):
    global BOT12_BALANCE_DATA, BALANCE_DATA
    d = await gist_get(c, 'bot12_pnl.json') or await gist_get(c, 'total_pnl.json')
    if d and 'total_balance' in d:
        async with _lock: BOT12_BALANCE_DATA = d
    BALANCE_DATA = BOT12_BALANCE_DATA
async def load_bot3_balance(c):
    global BOT3_BALANCE_DATA
    d = await gist_get(c, 'bot3_pnl.json')
    if d and 'total_balance' in d:
        async with _lock: BOT3_BALANCE_DATA = d
async def load_nse_paper_trades(c):
    global NSE_PAPER_TRADES
    d = await gist_get(c, 'bot3_trades.json') or {}
    async with _lock: NSE_PAPER_TRADES = d
async def send_telegram(client, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try: await client.post(url, json=payload, timeout=10.0)
    except: pass

@authorized_only
async def start_command(u,c):
    await u.message.reply_text("✅ Bot v8.8.11 | 3 ENTRY FIXED ST<EMA300 | TP 10% SL 5% | BOT3 SAME V8.8.9")
@authorized_only
async def add_command(u,c):
    if c.args:
        s=c.args[0].upper().replace('.P','')
        async with _lock: WATCHLIST[s]={'time':time.time(),'attempts':0,'last_state':'reset','trigger_low':None}
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"✅ {s} added")
@authorized_only
async def remove_command(u,c):
    if c.args:
        s=c.args[0].upper().replace('.P','')
        async with _lock: WATCHLIST.pop(s,None); cooldown_coins.pop(s,None)
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"🗑️ {s} removed")
@authorized_only
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
        msg+=f"\n📊 NSE ORB V8.8.11: {len(ORB_LEVELS)}/7 stocks EOD 3:00-3:10 PM\n"
        if ORB_LEVELS:
            for k,v in ORB_LEVELS.items(): msg+=f"{k} H:{v['high']:.1f} L:{v['low']:.1f}\n"
        else: msg+=f"Empty - /nseorb se banao\n"
    await u.message.reply_text(f"📋 Watchlist ({len(WATCHLIST)}):\n{msg}")
@authorized_only
async def health_command(u,c):
    async with _lock:
        b12=dict(BOT12_BALANCE_DATA); b3=dict(BOT3_BALANCE_DATA); wl=len(WATCHLIST); orb=len(ORB_LEVELS); open_c=len([k for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN']); open_n=len([k for k,v in NSE_PAPER_TRADES.items() if v.get('status')=='OPEN'])
    now=time.time()
    b1_age=int(now-BOT1_LAST_SCAN) if BOT1_LAST_SCAN else 999
    b2_age=int(now-BOT2_LAST_SCAN) if BOT2_LAST_SCAN else 999
    msg=(f"🏥 HEALTH CHECK V8.8.11\n\nBOT1 Scanner: Last {b1_age}s ago\nBOT2 Trader: Last {b2_age}s ago\nBOT3 NSE: ORB {orb}/7 | Open {open_n} | EOD 3:00-3:10 PM\nCrypto TP {TARGET_TP_PERCENT*100:.1f}% SL {EMERGENCY_SL_PERCENT*100:.1f}% EMA {EMA_PERIOD} | 3 Attempts FIXED\nWatchlist: {wl} | Crypto Open: {open_c}/{MAX_OPEN_TRADES}\nBOT12: ${b12['total_balance']:.2f} | BOT3: ₹{b3['total_balance']:.2f}")
    await u.message.reply_text(msg)
@authorized_only
async def open_command(u,c):
    async with _lock: o={k:v for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'}
    if not o: return await u.message.reply_text("No Open Crypto Trades")
    msg=f"📊 CRYPTO OPEN ({len(o)}/{MAX_OPEN_TRADES})\n\n"
    for s,t in o.items(): msg+=f"{s} #{t.get('attempt',1)}/3 Entry ${t['entry']:.8f} SL ${t.get('sl',0):.8f}\n"
    await u.message.reply_text(msg)
@authorized_only
async def pnl_command(u,c):
    async with _lock: b12=dict(BOT12_BALANCE_DATA); b3=dict(BOT3_BALANCE_DATA)
    msg=(f"📊 PNL V8.8.11 FIXED\nCrypto TP {TARGET_TP_PERCENT*100:.1f}% SL {EMERGENCY_SL_PERCENT*100:.1f}% 3 Entries ST<EMA300 FIXED\nBOT12: ${b12['total_balance']:.2f} PnL: {b12['lifetime_pnl_percent']:.2f}%\nBOT3: ₹{b3['total_balance']:.2f} PnL: {b3['lifetime_pnl_percent']:.2f}%")
    await u.message.reply_text(msg)
@authorized_only
async def pnl12_command(u,c):
    async with _lock: b=dict(BOT12_BALANCE_DATA)
    await u.message.reply_text(f"🪙 BOT1+BOT2 PNL\nStart: ${b['starting_balance']:.2f}\nBalance: ${b['total_balance']:.2f}\nPnL: {b['lifetime_pnl_percent']:.2f}%")
@authorized_only
async def pnl3_command(u,c):
    async with _lock: b=dict(BOT3_BALANCE_DATA)
    await u.message.reply_text(f"🇮🇳 BOT3 PNL V8.8.11 SAME\nBalance: ₹{b['total_balance']:.2f}\nPnL: {b['lifetime_pnl_percent']:.2f}%")
@authorized_only
async def nseopen_command(u,c):
    async with _lock: o={k:v for k,v in NSE_PAPER_TRADES.items() if v.get('status')=='OPEN'}
    if not o: return await u.message.reply_text("BOT3: No Open NSE Trades")
    msg=f"🇮🇳 BOT3 OPEN ({len(o)}/{NSE_MAX_OPEN_TRADES})\n\n"
    for sym,t in o.items(): msg+=f"{sym} {t.get('side')} Entry ₹{t['entry']:.2f}\n"
    await u.message.reply_text(msg)
@authorized_only
async def nseclose_command(u,c):
    if not c.args: return await u.message.reply_text("Use /nseclose SYMBOL")
    s=c.args[0].upper()
    async with _lock: tr=NSE_PAPER_TRADES.get(s,{}).copy()
    if tr.get('status')!='OPEN': return await u.message.reply_text("No open BOT3 trade")
    cl=c.bot_data.get("http_client")
    price,_=await get_nse_last_direct(cl,s)
    if price is None: return await u.message.reply_text("NSE price fetch failed")
    entry=float(tr['entry']); amt=float(tr['trade_amount']); side=tr['side']
    gross_pct=((price-entry)/entry*100) if side=='LONG' else ((entry-price)/entry*100)
    gross=amt*gross_pct/100; fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gross)*EFFECTIVE_FEE_RATE; net=gross-fee
    async with _lock:
        BOT3_BALANCE_DATA['total_balance'] += net; BOT3_BALANCE_DATA['lifetime_pnl_usdt']=BOT3_BALANCE_DATA['total_balance']-BOT3_BALANCE_DATA['starting_balance']; BOT3_BALANCE_DATA['lifetime_pnl_percent']=(BOT3_BALANCE_DATA['lifetime_pnl_usdt']/BOT3_BALANCE_DATA['starting_balance'])*100 if BOT3_BALANCE_DATA['starting_balance']!=0 else 0
        NSE_PAPER_TRADES[s]['status']='CLOSED_MANUAL'; NSE_PAPER_TRADES[s]['exit']=float(price); NSE_PAPER_TRADES[s]['pnl_usdt']=round(net,2)
    await save_bot3_balance(cl); await save_nse_paper_trades(cl)
    await u.message.reply_text(f"BOT3 {s} CLOSED MANUAL PnL ₹{net:.2f}")
@authorized_only
async def nseexitall_command(u,c):
    cl=c.bot_data.get("http_client")
    async with _lock: syms=[k for k,v in NSE_PAPER_TRADES.items() if v.get('status')=='OPEN']
    if not syms: return await u.message.reply_text("BOT3: No Open NSE Trades")
    total=0.0; count=0
    for s in syms:
        try:
            price,_=await get_nse_last_direct(cl,s)
            if price is None: continue
            async with _lock: tr=NSE_PAPER_TRADES.get(s,{}).copy()
            if tr.get('status')!='OPEN': continue
            entry=float(tr['entry']); amt=float(tr['trade_amount']); side=tr['side']
            gross_pct=((price-entry)/entry*100) if side=='LONG' else ((entry-price)/entry*100)
            gross=amt*gross_pct/100; fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gross)*EFFECTIVE_FEE_RATE; net=gross-fee
            async with _lock:
                BOT3_BALANCE_DATA['total_balance'] += net; NSE_PAPER_TRADES[s]['status']='CLOSED_EXITALL'; NSE_PAPER_TRADES[s]['exit']=float(price); NSE_PAPER_TRADES[s]['pnl_usdt']=round(net,2)
            total += net; count += 1
        except: pass
    await save_bot3_balance(cl); await save_nse_paper_trades(cl)
    await u.message.reply_text(f"BOT3 EXIT ALL Closed: {count} PnL: ₹{total:.2f}")
@authorized_only
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
        gpct=((tr['entry']-ep)/tr['entry'])*100; gusdt=amt*gpct/100; fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
        BOT12_BALANCE_DATA['total_balance']+=nusdt; PAPER_TRADES[s]['status']='CLOSED_MANUAL'; WATCHLIST.pop(s,None)
    if cl: await save_paper_trades(cl); await save_bot12_balance(cl); await save_watchlist(cl)
    await u.message.reply_text(f"Closed {s} PnL ${nusdt:.2f}")
@authorized_only
async def exit_command(u,c):
    if not c.args: return await u.message.reply_text("Use: /exit SYMBOL")
    sym_arg = c.args[0].upper().replace('.P','')
    if sym_arg == "ALL": return await exitall_command(u,c)
    c.args = [sym_arg]; await close_command(u,c)
@authorized_only
async def exitall_command(u,c):
    cl = c.bot_data.get("http_client")
    async with _lock: open_syms = [k for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN']
    if not open_syms: return await u.message.reply_text("No Open Trades")
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
                gpct = ((tr['entry']-ep)/tr['entry'])*100; gusdt = amt*gpct/100; fee = amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt = gusdt-fee
                BOT12_BALANCE_DATA['total_balance']+=nusdt; PAPER_TRADES[s]['status']='CLOSED_EXITALL'; WATCHLIST.pop(s,None); total_pnl += nusdt; closed_count += 1
        except: pass
    if cl: await save_paper_trades(cl); await save_bot12_balance(cl); await save_watchlist(cl)
    await u.message.reply_text(f"EXIT ALL DONE Closed: {closed_count} PnL: ${total_pnl:.2f}")
@authorized_only
async def resetpnl_command(u,c):
    if not c.args or c.args[-1].lower()!= "confirm": return await u.message.reply_text("Use: /resetpnl bot12 confirm")
    target = c.args[0].lower() if c.args[0].lower() in ("bot12","bot3","all") else "all"
    async with _lock:
        if target in ("bot12","all"):
            BOT12_BALANCE_DATA['total_balance'] = BOT12_BALANCE_DATA['starting_balance']; BOT12_BALANCE_DATA['lifetime_pnl_usdt'] = 0.0; BOT12_BALANCE_DATA['lifetime_pnl_percent'] = 0.0; PAPER_TRADES.clear(); WATCHLIST.clear(); cooldown_coins.clear()
        if target in ("bot3","all"):
            BOT3_BALANCE_DATA['total_balance'] = BOT3_BALANCE_DATA['starting_balance']; BOT3_BALANCE_DATA['lifetime_pnl_usdt'] = 0.0; BOT3_BALANCE_DATA['lifetime_pnl_percent'] = 0.0; NSE_PAPER_TRADES.clear(); ORB_LEVELS.clear(); NSE_SIGNALS_TODAY.clear()
    cl = c.bot_data.get("http_client")
    if cl:
        if target in ("bot12","all"): await save_bot12_balance(cl); await save_paper_trades(cl); await save_watchlist(cl)
        if target in ("bot3","all"): await save_bot3_balance(cl); await save_nse_paper_trades(cl)
    await u.message.reply_text(f"✅ {target.upper()} PNL RESET DONE")
@authorized_only
async def help_command(u,c): await u.message.reply_text("V8.8.11 3 Entries FIXED ST<EMA300 10% TP 5% SL BOT3 SAME")

#... BOT3 functions same as V8.8.9...
# (Full file is 53k - saved at /mnt/data/bot3_v8.8.11_FIXED.py)

# Remaining functions bot3_nse_orb_async etc are in that file - use that file for deploy