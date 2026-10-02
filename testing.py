# COINDEX V8.8.28 FINAL - 3.2GB/mo + 100% RULE MATCH - 7 FIXES
import threading, asyncio, httpx, time, os, json, pandas as pd, numpy as np, logging, functools
from decimal import Decimal, ROUND_DOWN
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

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
BOT1_SCAN_INTERVAL = 300
BOT2_SCAN_INTERVAL = 30
KLINE_LIMIT = 200
KLINE_CACHE_TTL = 600
LIVE_PRICE_CACHE_TTL = 60
GRACE_AFTER_PUMP = 1800
BOT12_STARTING_BALANCE = 10000.0

def get_grace_by_pump(pump_pct):
    if pump_pct >= 100: return 3600
    elif pump_pct >= 60: return 2400
    else: return 1800

TAKER_FEE = 0.0005
GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)
SLIPPAGE_PCT = 0.001
MAX_DISTANCE_PCT = 0.005
WEBHOOK_SECRET = os.environ.get("TELEGRAM_SECRET", "change_this_secret_123")
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
KLINE_CACHE = {}; LIVE_PRICE_CACHE = {}
cooldown_coins = {}
_lock = asyncio.Lock(); _gist_lock = asyncio.Lock()
BOT12_BALANCE_DATA = {"total_balance": BOT12_STARTING_BALANCE, "starting_balance": BOT12_STARTING_BALANCE, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
application = None; main_event_loop = None; webhook_queue = asyncio.Queue()
BOT1_LAST_SCAN = 0; BOT2_LAST_SCAN = 0

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
        if not TELEGRAM_CHAT_ID: return await func(update, context, *args, **kwargs)
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
                    WATCHLIST[cs].setdefault('skip_until',0)
                    WATCHLIST[cs].setdefault('pump_pct',0)
                    WATCHLIST[cs].setdefault('trigger_bar_time',0)
                    WATCHLIST[cs].setdefault('trigger_time',0)

async def load_paper_trades(c):
    global PAPER_TRADES
    data = await gist_get(c, 'paper_trades_crypto.json') or await gist_get(c, 'paper_trades.json') or {}
    async with _lock: PAPER_TRADES = data

async def load_bot12_balance(c):
    global BOT12_BALANCE_DATA
    d = await gist_get(c, 'bot12_pnl.json') or await gist_get(c, 'total_pnl.json')
    if d and 'total_balance' in d:
        async with _lock: BOT12_BALANCE_DATA = d

async def send_telegram(client, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try: await client.post(url, json=payload, timeout=10.0)
    except: pass

@authorized_only
async def start_command(u,c): await u.message.reply_text("V8.8.28 FINAL 3.2GB RuleMatch Dynamic")
@authorized_only
async def add_command(u,c):
    if c.args:
        s=c.args[0].upper().replace('.P','')
        async with _lock: WATCHLIST[s]={'time':time.time(),'skip_until':time.time()+GRACE_AFTER_PUMP,'attempts':0,'last_state':'reset','trigger_low':None,'pump_pct':0,'trigger_bar_time':0,'trigger_time':0}
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"{s} added 30m grace")
@authorized_only
async def remove_command(u,c):
    if c.args:
        s=c.args[0].upper().replace('.P','')
        async with _lock: WATCHLIST.pop(s,None); cooldown_coins.pop(s,None); KLINE_CACHE.pop(s,None); LIVE_PRICE_CACHE.pop(s,None)
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"{s} removed")
@authorized_only
async def watchlist_command(u,c):
    msg=""; now=time.time()
    async with _lock:
        for s,d in WATCHLIST.items():
            skip = d.get('skip_until',0)
            grace=f" {int((skip-now)/60)}m grace" if skip>now else ""
            msg+=f"{s} #{d.get('attempts',0)+1} {d.get('last_state')}{grace}\n"
    await u.message.reply_text(f"WL({len(WATCHLIST)}) V8.8.28 FINAL:\n{msg}")
@authorized_only
async def health_command(u,c):
    async with _lock: b12=dict(BOT12_BALANCE_DATA); wl=len(WATCHLIST); open_c=len([k for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'])
    now=time.time(); b1_age=int(now-BOT1_LAST_SCAN) if BOT1_LAST_SCAN else 999; b2_age=int(now-BOT2_LAST_SCAN) if BOT2_LAST_SCAN else 999
    await u.message.reply_text(f"HEALTH V8.8.28 FINAL RuleMatch\nBOT1:{b1_age}s 300s | BOT2:{b2_age}s 30s\nWL:{wl} Open:{open_c}/4 Bal:${b12['total_balance']:.2f}")
@authorized_only
async def open_command(u,c):
    async with _lock: o={k:v for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'}
    if not o: return await u.message.reply_text("No Open")
    msg=f"OPEN ({len(o)})\n"
    for s,t in o.items(): msg+=f"{s} Entry ${t['entry']:.8f}\n"
    await u.message.reply_text(msg)
@authorized_only
async def pnl_command(u,c):
    async with _lock: b=dict(BOT12_BALANCE_DATA)
    await u.message.reply_text(f"PNL V8.8.28 FINAL ${b['total_balance']:.2f} {b['lifetime_pnl_percent']:.2f}%")
@authorized_only
async def pnl12_command(u,c):
    async with _lock: b=dict(BOT12_BALANCE_DATA)
    await u.message.reply_text(f"BOT12 ${b['total_balance']:.2f} {b['lifetime_pnl_percent']:.2f}%")
@authorized_only
async def pnl3_command(u,c): await u.message.reply_text("BOT3 DISABLED")
@authorized_only
async def nseopen_command(u,c): await u.message.reply_text("BOT3 DISABLED")
@authorized_only
async def nseclose_command(u,c): await u.message.reply_text("BOT3 DISABLED")
@authorized_only
async def nseexitall_command(u,c): await u.message.reply_text("BOT3 DISABLED")
@authorized_only
async def nseorb_command(u,c): await u.message.reply_text("BOT3 DISABLED")
@authorized_only
async def close_command(u,c):
    if not c.args: return await u.message.reply_text("Use /close SYM")
    s=c.args[0].upper().replace('.P','')
    cl=c.bot_data.get("http_client")
    async with _lock:
        if s not in PAPER_TRADES or PAPER_TRADES[s]['status']!='OPEN': return await u.message.reply_text("No open")
        tr=PAPER_TRADES[s].copy()
    df=await get_klines_cached(cl, s)
    if df is None: return await u.message.reply_text("Fetch failed")
    ep=df['close'].iloc[-1]
    async with _lock:
        amt = tr.get('trade_amount_usdt', tr['balance_at_entry']*0.20)
        gpct=((tr['entry']-ep)/tr['entry'])*100; gusdt=amt*gpct/100; fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
        BOT12_BALANCE_DATA['total_balance']+=nusdt; PAPER_TRADES[s]['status']='CLOSED_MANUAL'; WATCHLIST.pop(s,None); KLINE_CACHE.pop(s,None); LIVE_PRICE_CACHE.pop(s,None)
    if cl: await save_paper_trades(cl); await save_bot12_balance(cl); await save_watchlist(cl)
    await u.message.reply_text(f"Closed {s} ${nusdt:.2f}")
@authorized_only
async def exit_command(u,c):
    if not c.args: return await u.message.reply_text("Use /exit SYM/ALL")
    if c.args[0].upper()=="ALL": return await exitall_command(u,c)
    await close_command(u,c)
@authorized_only
async def exitall_command(u,c):
    cl = c.bot_data.get("http_client")
    async with _lock: open_syms = [k for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN']
    if not open_syms: return await u.message.reply_text("No Open")
    total=0; cnt=0
    for s in open_syms:
        try:
            async with _lock: tr = PAPER_TRADES.get(s,{}).copy()
            df = await get_klines_cached(cl, s)
            if df is None: continue
            ep = df['close'].iloc[-1]
            async with _lock:
                amt = tr.get('trade_amount_usdt', tr['balance_at_entry']*0.20)
                gpct=((tr['entry']-ep)/tr['entry'])*100; gusdt=amt*gpct/100; fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
                BOT12_BALANCE_DATA['total_balance']+=nusdt; PAPER_TRADES[s]['status']='CLOSED_EXITALL'; WATCHLIST.pop(s,None); KLINE_CACHE.pop(s,None); LIVE_PRICE_CACHE.pop(s,None); total+=nusdt; cnt+=1
        except: pass
    if cl: await save_paper_trades(cl); await save_bot12_balance(cl); await save_watchlist(cl)
    await u.message.reply_text(f"EXIT ALL {cnt} ${total:.2f}")
@authorized_only
async def resetpnl_command(u,c):
    if not c.args or c.args[-1].lower()!= "confirm": return await u.message.reply_text("Use /resetpnl bot12 confirm")
    async with _lock:
        BOT12_BALANCE_DATA['total_balance'] = BOT12_BALANCE_DATA['starting_balance']
        BOT12_BALANCE_DATA['lifetime_pnl_usdt'] = 0.0
        BOT12_BALANCE_DATA['lifetime_pnl_percent'] = 0.0
    cl = c.bot_data.get("http_client")
    if cl: await save_bot12_balance(cl)
    await u.message.reply_text("PNL RESET")
@authorized_only
async def help_command(u,c): await u.message.reply_text("V8.8.28 FINAL | RuleMatch | BOT1 300s | K200 Cache10m | BOT2 30s | 3.2GB/mo")

async def get_klines_bybit_async(client, symbol, interval='5', limit=200, include_current=False):
    url="https://api.bybit.com/v5/market/kline"; by=symbol if symbol.endswith('USDT') else f"{symbol}USDT"; params={'category':'linear','symbol':by,'interval':interval,'limit':limit}
    try:
        res=await client.get(url, params=params, timeout=10.0); data=res.json()
        if data.get('retCode')==0 and data['result']['list']:
            df=pd.DataFrame(data['result']['list'], columns=['timestamp','open','high','low','close','volume','turnover']); df=df.astype({'timestamp':'int64','open':float,'high':float,'low':float,'close':float}); df=df.iloc[::-1].reset_index(drop=True)
            if not include_current: df=df.iloc[:-1].reset_index(drop=True)
            if len(df)>=50: return df
    except: pass
    return None

async def get_tick_size(client, symbol):
    if symbol in TICK_CACHE: return TICK_CACHE[symbol]
    url="https://api.bybit.com/v5/market/instruments-info"; by=symbol if symbol.endswith('USDT') else f"{symbol}USDT"; params={'category':'linear','symbol':by}
    try:
        res=await client.get(url, params=params, timeout=10.0); data=res.json()
        if data.get('retCode')==0 and data['result']['list']:
            tick=float(data['result']['list'][0]['priceFilter']['tickSize'])
            if tick > 0: TICK_CACHE[symbol]=tick; return tick
    except: return None
    return None

async def get_live_price(client, symbol):
    url="https://api.bybit.com/v5/market/tickers"; by=symbol if symbol.endswith('USDT') else f"{symbol}USDT"; params={'category':'linear','symbol':by}
    try:
        res=await client.get(url, params=params, timeout=5.0); data=res.json()
        if data.get('retCode')==0 and data['result']['list']:
            lp = float(data['result']['list'][0]['lastPrice']); return lp if lp > 0 else None
    except: pass
    return None

async def get_klines_cached(client, symbol):
    now=time.time()
    if symbol in KLINE_CACHE:
        e=KLINE_CACHE[symbol]
        if now - e['time'] < KLINE_CACHE_TTL: return e['df']
    df=await get_klines_bybit_async(client, symbol, interval='5', limit=KLINE_LIMIT, include_current=True)
    if df is not None: KLINE_CACHE[symbol]={'df':df,'time':now}
    return df

async def get_live_price_cached(client, symbol):
    now=time.time()
    if symbol in LIVE_PRICE_CACHE:
        e=LIVE_PRICE_CACHE[symbol]
        if now - e['time'] < LIVE_PRICE_CACHE_TTL: return e['price']
    price=await get_live_price(client, symbol)
    if price and price>0: LIVE_PRICE_CACHE[symbol]={'price':price,'time':now}
    return price

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy(); high, low, close = df['high'].values, df['low'].values, df['close'].values; hl2 = (high + low) / 2.0; h_l = high - low; h_pc = np.abs(high - np.roll(close, 1)); l_pc = np.abs(low - np.roll(close, 1)); h_pc[0] = h_l[0]; l_pc[0] = h_l[0]; tr = np.maximum(h_l, np.maximum(h_pc, l_pc)); atr = np.zeros(len(tr)); atr[0] = np.mean(tr[:period])
    for i in range(1, len(tr)): atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    upperband = hl2 + (multiplier * atr); lowerband = hl2 - (multiplier * atr); n = len(df); final_upperband = np.zeros(n); final_lowerband = np.zeros(n); supertrend = np.ones(n, dtype=bool); st_line = np.zeros(n)
    for i in range(n):
        if i == 0: final_upperband[i]=upperband[i]; final_lowerband[i]=lowerband[i]; st_line[i]=upperband[i]; supertrend[i]=False; continue
        if upperband[i] < final_upperband[i-1] or close[i-1] > final_upperband[i-1]: final_upperband[i]=upperband[i]
        else: final_upperband[i]=final_upperband[i-1]
        if lowerband[i] > final_lowerband[i-1] or close[i-1] < final_lowerband[i-1]: final_lowerband[i]=lowerband[i]
        else: final_lowerband[i]=final_lowerband[i-1]
        prev_st=supertrend[i-1]
        if prev_st and close[i] < final_lowerband[i]: supertrend[i]=False
        elif not prev_st and close[i] > final_upperband[i]: supertrend[i]=True
        else: supertrend[i]=prev_st
        if supertrend[i]: st_line[i]=final_lowerband[i]
        else: st_line[i]=final_upperband[i]
    df['st_line']=st_line
    df['ema_val'] = df['close'].ewm(span=300, adjust=False).mean()
    return df

async def check_paper_trades(client, df_live, df_closed, symbol):
    try:
        async with _lock:
            if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!= 'OPEN': return
            trade = PAPER_TRADES[symbol].copy()
        clow = min(float(df_closed['low'].iloc[-1]), float(df_live['low'].iloc[-1]))
        chigh = max(float(df_closed['high'].iloc[-1]), float(df_live['high'].iloc[-1]))
        entry = trade['entry']; attempt = trade.get('attempt', 1)
        amt_orig = trade.get('trade_amount_usdt', trade['balance_at_entry'] * 0.20)
        if chigh >= trade['sl']:
            fee = amt_orig*EFFECTIVE_FEE_RATE + max(0, amt_orig + amt_orig*((entry-trade['sl'])/entry)*100/100)*EFFECTIVE_FEE_RATE
            nusdt = amt_orig*((entry-trade['sl'])/entry*100)/100 - fee
            async with _lock:
                BOT12_BALANCE_DATA['total_balance'] += nusdt
                BOT12_BALANCE_DATA['lifetime_pnl_usdt'] = BOT12_BALANCE_DATA['total_balance'] - BOT12_BALANCE_DATA['starting_balance']
                BOT12_BALANCE_DATA['lifetime_pnl_percent'] = (BOT12_BALANCE_DATA['lifetime_pnl_usdt']/BOT12_BALANCE_DATA['starting_balance'])*100 if BOT12_BALANCE_DATA['starting_balance']!=0 else 0
                PAPER_TRADES[symbol]['status']='CLOSED_SL'; PAPER_TRADES[symbol]['pnl_usdt']=round(nusdt,2)
                if attempt==1: WATCHLIST[symbol]['attempts']=1; WATCHLIST[symbol]['last_state']='waiting_st_bullish'; WATCHLIST[symbol]['trigger_low']=None; WATCHLIST[symbol]['trigger_bar_time']=0; WATCHLIST[symbol]['trigger_time']=0; rmsg=f"SL {symbol} #{attempt}/3"
                elif attempt==2: WATCHLIST[symbol]['attempts']=2; WATCHLIST[symbol]['last_state']='waiting_st_bullish'; WATCHLIST[symbol]['trigger_low']=None; WATCHLIST[symbol]['trigger_bar_time']=0; WATCHLIST[symbol]['trigger_time']=0; rmsg=f"SL {symbol} #{attempt}/3"
                else: WATCHLIST.pop(symbol,None); cooldown_coins[symbol]=time.time()+3*3600; rmsg=f"SL {symbol} #{attempt}/3 cooldown"; KLINE_CACHE.pop(symbol,None); LIVE_PRICE_CACHE.pop(symbol,None)
            await save_bot12_balance(client); await save_paper_trades(client); await save_watchlist(client); asyncio.create_task(send_telegram(client,rmsg)); return
        if clow <= trade['tp']:
            gusdt = amt_orig*((entry-trade['tp'])/entry*100)/100; fee=amt_orig*EFFECTIVE_FEE_RATE + (amt_orig+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
            async with _lock:
                BOT12_BALANCE_DATA['total_balance']+=nusdt; BOT12_BALANCE_DATA['lifetime_pnl_usdt']=BOT12_BALANCE_DATA['total_balance']-BOT12_BALANCE_DATA['starting_balance']; BOT12_BALANCE_DATA['lifetime_pnl_percent']=(BOT12_BALANCE_DATA['lifetime_pnl_usdt']/BOT12_BALANCE_DATA['starting_balance'])*100 if BOT12_BALANCE_DATA['starting_balance']!=0 else 0
                PAPER_TRADES[symbol]['status']='CLOSED_TP'; PAPER_TRADES[symbol]['pnl_usdt']=round(nusdt,2); WATCHLIST.pop(symbol,None); cooldown_coins[symbol]=time.time()+4*3600; KLINE_CACHE.pop(symbol,None); LIVE_PRICE_CACHE.pop(symbol,None)
            await save_bot12_balance(client); await save_paper_trades(client); await save_watchlist(client)
            asyncio.create_task(send_telegram(client,f"TP {symbol} ${nusdt:.2f}")); return
    except Exception as e: print(f"check trades {symbol}: {e}", flush=True)

async def bot1_scan(client):
    global BOT1_LAST_SCAN
    print("Bot1 V8.8.28 FINAL RuleMatch 30/40/60", flush=True)
    while True:
        try:
            BOT1_LAST_SCAN=time.time()
            url="https://api.bybit.com/v5/market/tickers?category=linear"
            res=await client.get(url, timeout=20.0); data=res.json(); added=0
            if data.get('retCode')==0 and data.get('result'):
                for t in data['result']['list']:
                    m=t.get('symbol','')
                    if not m.endswith('USDT'): continue
                    s=m.replace('.P','')
                    try: ch=float(t.get('price24hPcnt',0))*100; turnover=float(t.get('turnover24h',0))
                    except: continue
                    if turnover < MIN_TURNOVER_24H or ch < PUMP_PERCENT_24H: continue
                    async with _lock:
                        if s not in WATCHLIST:
                            if s in cooldown_coins and time.time() < cooldown_coins[s]: continue
                            if s in cooldown_coins: del cooldown_coins[s]
                            grace = get_grace_by_pump(ch)
                            WATCHLIST[s]={'time':time.time(),'skip_until':time.time()+grace,'attempts':0,'last_state':'reset','trigger_low':None,'pump_pct':ch,'trigger_bar_time':0,'trigger_time':0}; added+=1
                            asyncio.create_task(send_telegram(client,f"PUMP {s} +{ch:.1f}% | Scan {grace//60}m later"))
            if added>0: await save_watchlist(client)
        except Exception as e: print(f"Bot1 {e}", flush=True)
        await asyncio.sleep(BOT1_SCAN_INTERVAL)

async def process_symbol(client, symbol):
    global BOT2_LAST_SCAN
    try:
        BOT2_LAST_SCAN=time.time()
        df_live_raw=await get_klines_cached(client, symbol)
        if df_live_raw is None or len(df_live_raw) < 200: return False
        df_closed_raw=df_live_raw.iloc[:-1].reset_index(drop=True)
        df_live=await asyncio.to_thread(calculate_supertrend, df_live_raw)
        df_closed=await asyncio.to_thread(calculate_supertrend, df_closed_raw)
        await check_paper_trades(client, df_live, df_closed, symbol)
        low_live=float(df_live['low'].iloc[-1]); close_closed=float(df_closed['close'].iloc[-1]); ema_closed=float(df_closed['ema_val'].iloc[-1]); prev_ema_closed=float(df_closed['ema_val'].iloc[-2]); st_closed=float(df_closed['st_line'].iloc[-1]); prev_st_closed=float(df_closed['st_line'].iloc[-2]); low_closed=float(df_closed['low'].iloc[-1])
        changed=False; new=False; msg=None
        tick=await get_tick_size(client, symbol)
        if tick is None or tick<=0: return False
        raw_live=await get_live_price_cached(client, symbol)
        if raw_live is None or raw_live<=0: return False
        live=float(raw_live)
        curr_bar_ts = int(df_closed['timestamp'].iloc[-1])
        async with _lock:
            if symbol not in WATCHLIST: return False
            if WATCHLIST[symbol].get('skip_until',0) > time.time(): return False
            if PAPER_TRADES.get(symbol,{}).get('status')=='OPEN': return False
            att=WATCHLIST[symbol].get('attempts',0); state=WATCHLIST[symbol].get('last_state','reset'); active=sum(1 for t in PAPER_TRADES.values() if t.get('status')=='OPEN')
            # 2nd/3rd ENTRY WAIT LOGIC
            if att in [1,2]:
                if state=='waiting_st_bullish' and (close_closed>st_closed or live>st_closed):
                    WATCHLIST[symbol]['last_state']='waiting_st_bearish'; changed=True; asyncio.create_task(send_telegram(client,f"ST Bull {symbol} wait Bear #{att+1}")); return changed
                if state=='waiting_st_bearish' and (close_closed<st_closed or live<st_closed):
                    WATCHLIST[symbol]['last_state']='reset'; WATCHLIST[symbol]['trigger_low']=None; WATCHLIST[symbol]['trigger_bar_time']=0; changed=True; asyncio.create_task(send_telegram(client,f"ST Bear {symbol} ready #{att+1}")); return changed
            trig=WATCHLIST[symbol].get('trigger_low')
            trig_bar=WATCHLIST[symbol].get('trigger_bar_time',0)
            trig_time=WATCHLIST[symbol].get('trigger_time',0)
            # FIX: CANCEL IF PRICE > ST OR > EMA (OR logic)
            if trig is not None and trig>0:
                if time.time() - trig_time > 60:
                    if close_closed > st_closed or close_closed > ema_closed:
                        WATCHLIST[symbol]['trigger_low']=None; WATCHLIST[symbol]['trigger_bar_time']=0; WATCHLIST[symbol]['trigger_time']=0; WATCHLIST[symbol]['last_state']='reset'; changed=True
                        asyncio.create_task(send_telegram(client,f"Cancel Trig {symbol} price above ST/EMA")); return changed
                # SAME CANDLE BLOCK
                if curr_bar_ts == trig_bar:
                    return False
            should=False; exec_price=0.0
            if trig is not None and trig>0:
                if curr_bar_ts > trig_bar:
                    if low_live <= trig - (tick*TRIGGER_TICKS):
                        should=True; exec_price=(trig - (tick*TRIGGER_TICKS))*(1-SLIPPAGE_PCT)
                        if exec_price>0 and att!=0 and abs(live-exec_price)/exec_price > MAX_DISTANCE_PCT: should=False
            else:
                # FRESH CROSS ONLY - PURE RULE
                st_cross = (prev_st_closed >= prev_ema_closed) and (st_closed < ema_closed)
                price_below = (close_closed < st_closed) and (close_closed < ema_closed)
                if st_cross and price_below and low_closed>0:
                    WATCHLIST[symbol]['trigger_low']=low_closed; WATCHLIST[symbol]['last_state']=f'waiting_break_{att+1}'; WATCHLIST[symbol]['trigger_time']=time.time(); WATCHLIST[symbol]['trigger_bar_time']=curr_bar_ts; changed=True
                    asyncio.create_task(send_telegram(client,f"{att+1}st Trig {symbol} Low ${low_closed:.8f}"))
            if should and att<3 and active<MAX_OPEN_TRADES and exec_price>0:
                ep=price_to_tick(exec_price,tick); tp=price_to_tick(ep*(1-TARGET_TP_PERCENT),tick); sl=price_to_tick(ep*(1+EMERGENCY_SL_PERCENT),tick)
                if ep<=0 or tp<=0 or sl<=0: return changed
                tamt=BOT12_BALANCE_DATA['total_balance']*POSITION_SIZE_PERCENT; cur=att+1
                WATCHLIST[symbol]['attempts']=cur; WATCHLIST[symbol]['last_state']='short'; WATCHLIST[symbol]['trigger_low']=None; WATCHLIST[symbol]['trigger_bar_time']=0; WATCHLIST[symbol]['trigger_time']=0; WATCHLIST[symbol]['skip_until']=0
                PAPER_TRADES[symbol]={'entry':ep,'tp':tp,'sl':sl,'status':'OPEN','time':time.time(),'balance_at_entry':BOT12_BALANCE_DATA['total_balance'],'trade_amount_usdt':tamt,'attempt':cur}
                msg=f"SHORT #{cur} {symbol} Entry ${ep:.8f} TP -10% SL 5%"; new=True; changed=True
        if new: await save_paper_trades(client); asyncio.create_task(send_telegram(client,msg))
        return changed
    except Exception as e: print(f"proc {symbol} {e}", flush=True); return False

async def bot2_scan(client):
    print("Bot2 V8.8.28 FINAL RuleMatch", flush=True)
    sem=asyncio.Semaphore(10)
    async def limited(s):
        async with sem: return await process_symbol(client,s)
    while True:
        try:
            now=time.time()
            async with _lock:
                active_syms=[]
                for s,d in WATCHLIST.items():
                    if d.get('skip_until',0) > now: continue
                    if d.get('trigger_low') is not None: active_syms.append(s)
                    elif PAPER_TRADES.get(s,{}).get('status')=='OPEN': active_syms.append(s)
                    elif d.get('last_state') in ['waiting_st_bullish','waiting_st_bearish']: active_syms.append(s)
                    elif d.get('attempts',0)==0 and d.get('last_state')=='reset': active_syms.append(s)
                syms=list(active_syms)
            if not syms: await asyncio.sleep(10); continue
            results=await asyncio.gather(*[limited(s) for s in syms])
            if any(results): await save_watchlist(client)
        except Exception as e: print(f"Bot2 {e}", flush=True)
        await asyncio.sleep(BOT2_SCAN_INTERVAL)

@app.route('/')
def home(): return jsonify({"status":"v8.8.28 FINAL RuleMatch 3.2GB","watchlist":len(WATCHLIST)})
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if WEBHOOK_SECRET!="change_this_secret_123":
            if request.headers.get("X-Telegram-Bot-Api-Secret-Token")!=WEBHOOK_SECRET: return jsonify({"ok":False}),403
        data=request.get_json(force=True,silent=True)
        if data and main_event_loop: main_event_loop.call_soon_threadsafe(webhook_queue.put_nowait,data)
    except: pass
    return jsonify({"ok":True}),200
async def process_webhook_queue():
    while True:
        data=await webhook_queue.get()
        try:
            if application and application.bot:
                update=Update.de_json(data,application.bot)
                await application.process_update(update)
        except: pass
        finally: webhook_queue.task_done()
async def main_async():
    global application, main_event_loop
    main_event_loop=asyncio.get_running_loop()
    limits=httpx.Limits(max_keepalive_connections=20,max_connections=100)
    async with httpx.AsyncClient(limits=limits) as client:
        await load_watchlist(client); await load_paper_trades(client); await load_bot12_balance(client)
        print(f"Loaded V8.8.28 FINAL {len(WATCHLIST)} Bal ${BOT12_BALANCE_DATA['total_balance']:.2f}", flush=True)
        t_req=HTTPXRequest(connection_pool_size=20,connect_timeout=30.0,read_timeout=30.0,write_timeout=30.0)
        app_t=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
        app_t.bot_data["http_client"]=client
        application=app_t
        for cmd,fn in [("start",start_command),("add",add_command),("remove",remove_command),("watchlist",watchlist_command),("open",open_command),("close",close_command),("pnl",pnl_command),("pnl12",pnl12_command),("pnl3",pnl3_command),("nseopen",nseopen_command),("nseclose",nseclose_command),("nseexitall",nseexitall_command),("exit",exit_command),("exitall",exitall_command),("resetpnl",resetpnl_command),("reset",resetpnl_command),("help",help_command),("nseorb",nseorb_command),("health",health_command)]:
            app_t.add_handler(CommandHandler(cmd,fn))
        await app_t.initialize(); await app_t.start()
        asyncio.create_task(process_webhook_queue())
        if WEBHOOK_URL:
            wh_url=f"{WEBHOOK_URL.rstrip('/')}/webhook"
            try: await app_t.bot.delete_webhook(drop_pending_updates=True); await asyncio.sleep(1); await app_t.bot.set_webhook(url=wh_url,drop_pending_updates=True,secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET!="change_this_secret_123" else None)
            except: pass
        else:
            try: await app_t.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            await asyncio.sleep(5); await app_t.updater.start_polling(drop_pending_updates=True,poll_interval=2.0,bootstrap_retries=-1)
        port=int(os.environ.get("PORT",10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0',port=port,use_reloader=False),daemon=True).start()
        asyncio.create_task(bot1_scan(client)); asyncio.create_task(bot2_scan(client))
        print("v8.8.28 FINAL Operational RuleMatch 3.2GB", flush=True)
        while True: await asyncio.sleep(3600)
def main():
    loop=asyncio.get_event_loop()
    loop.run_until_complete(main_async())
if __name__=='__main__': main()