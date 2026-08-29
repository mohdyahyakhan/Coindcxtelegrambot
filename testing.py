# COINDEX V8.7.4 - EMA 2 PIP ENTRY FIX
# #1: Live price < EMA 300 = Instant Entry -> Entry Price = EMA - 2 Pip

import threading, asyncio, httpx, time, os, json, pandas as pd, math, logging, traceback
from decimal import Decimal, ROUND_DOWN
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

PUMP_PERCENT_24H = 40
PIP_SIZE = 2
TARGET_TP_PERCENT = 0.05
EMERGENCY_SL_PERCENT = 0.02
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
POSITION_SIZE_PERCENT = 0.20
WATCHLIST_DAYS = 2
MAX_OPEN_TRADES = 4
MIN_TURNOVER_24H = 2000000
TAKER_FEE = 0.0005
GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)

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

async def save_watchlist(c): await gist_set_locked(c, 'watchlist.json', {'coins': WATCHLIST})
async def save_paper_trades(c): await gist_set_locked(c, 'paper_trades.json', PAPER_TRADES)
async def save_balance_data(c): await gist_set_locked(c, 'total_pnl.json', BALANCE_DATA)

async def load_watchlist(c):
    global WATCHLIST
    data=await gist_get(c, 'watchlist.json')
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
    PAPER_TRADES=await gist_get(c, 'paper_trades.json') or {}
async def load_balance_data(c):
    global BALANCE_DATA
    d=await gist_get(c, 'total_pnl.json')
    if d and 'total_balance' in d: BALANCE_DATA=d

async def send_telegram(client, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try: await client.post(url, json=payload, timeout=10.0)
    except: pass

async def start_command(u,c):
    await u.message.reply_text("✅ Bot v8.7.4 EMA 2PIP FIX ACTIVE", parse_mode="HTML")
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
        async with _lock:
            WATCHLIST.pop(s,None)
            cooldown_coins.pop(s,None)
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"🗑️ {s} removed + cooldown cleared", parse_mode="HTML")
async def watchlist_command(u,c):
    msg=""
    for s,d in WATCHLIST.items():
        trig=d.get('trigger_low')
        msg+=f"{s} #{d.get('attempts',0)+1} {d.get('last_state')} Trig:{trig}\n"
    if cooldown_coins:
        msg+="\n⏳ Cooldown:\n"
        for s,ts in cooldown_coins.items():
            rem=max(0,(ts-time.time())/3600)
            msg+=f"{s} {rem:.1f}hr left\n"
    if not msg: msg="Empty"
    await u.message.reply_text(f"📋 Watchlist ({len(WATCHLIST)}):\n{msg}", parse_mode="HTML")
async def open_command(u,c):
    o={k:v for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'}
    if not o: return await u.message.reply_text("No Open Trades", parse_mode="HTML")
    msg=f"📊 OPEN ({len(o)}/{MAX_OPEN_TRADES})\n\n"
    for s,t in o.items(): msg+=f"{s} #{t.get('attempt',1)}/3 Entry ${t['entry']:.8f}\n"
    await u.message.reply_text(msg, parse_mode="HTML")
async def pnl_command(u,c):
    b=BALANCE_DATA
    await u.message.reply_text(f"Balance: ${b['total_balance']:.2f} PnL: {b['lifetime_pnl_percent']:.2f}%", parse_mode="HTML")
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
        ratio=0.5 if tr.get('tp1_hit') else 1.0
        amt=tr.get('trade_amount_usdt', tr['balance_at_entry']*POSITION_SIZE_PERCENT)*ratio
        gpct=((tr['entry']-ep)/tr['entry'])*100; gusdt=amt*gpct/100
        fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
        BALANCE_DATA['total_balance']+=nusdt
        BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
        BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
        PAPER_TRADES[s]['status']='CLOSED_MANUAL'; PAPER_TRADES[s]['pnl_percent']=round((nusdt/amt)*100,2) if amt>0 else 0; PAPER_TRADES[s]['pnl_usdt']=round(nusdt,2)
        WATCHLIST.pop(s,None)
    if cl: await save_paper_trades(cl); await save_balance_data(cl); await save_watchlist(cl)
    await u.message.reply_text(f"Closed {s} PnL ${nusdt:.2f}", parse_mode="HTML")

async def get_klines_bybit_async(client, symbol, interval='5', limit=400, include_current=False):
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
            TICK_CACHE[symbol]=tick
            return tick
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

async def get_klines(client, symbol, interval='5', limit=400, include_current=False):
    return await get_klines_bybit_async(client, symbol, interval, limit, include_current)

def calculate_supertrend(df, period=10, multiplier=3):
    df=df.copy()
    df['h-l']=df['high']-df['low']; df['h-pc']=abs(df['high']-df['close'].shift(1)); df['l-pc']=abs(df['low']-df['close'].shift(1))
    df['tr']=df[['h-l','h-pc','l-pc']].max(axis=1); df['atr']=df['tr'].ewm(alpha=1/period, adjust=False).mean()
    hl2=(df['high']+df['low'])/2; df['upperband']=hl2+(multiplier*df['atr']); df['lowerband']=hl2-(multiplier*df['atr'])
    df['final_upperband']=0.0; df['final_lowerband']=0.0; df['supertrend']=True; df['st_line']=0.0; df['st_dir']=0
    for i in range(len(df)):
        if i==0:
            df.loc[df.index[i],'final_upperband']=df['upperband'].iloc[i]; df.loc[df.index[i],'final_lowerband']=df['lowerband'].iloc[i]; df.loc[df.index[i],'st_line']=df['upperband'].iloc[i]; df.loc[df.index[i],'st_dir']=1; continue
        if df['upperband'].iloc[i] < df['final_upperband'].iloc[i-1] or df['close'].iloc[i-1] > df['final_upperband'].iloc[i-1]: df.loc[df.index[i],'final_upperband']=df['upperband'].iloc[i]
        else: df.loc[df.index[i],'final_upperband']=df['final_upperband'].iloc[i-1]
        if df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i-1] or df['close'].iloc[i-1] < df['final_lowerband'].iloc[i-1]: df.loc[df.index[i],'final_lowerband']=df['lowerband'].iloc[i]
        else: df.loc[df.index[i],'final_lowerband']=df['final_lowerband'].iloc[i-1]
        prev_st=df['supertrend'].iloc[i-1]; close_i=df['close'].iloc[i]
        if prev_st and close_i < df['final_lowerband'].iloc[i]: df.loc[df.index[i],'supertrend']=False
        elif not prev_st and close_i > df['final_upperband'].iloc[i]: df.loc[df.index[i],'supertrend']=True
        else: df.loc[df.index[i],'supertrend']=prev_st
        if df['supertrend'].iloc[i]:
            df.loc[df.index[i],'st_line']=df['final_lowerband'].iloc[i]
            df.loc[df.index[i],'st_dir']=-1
        else:
            df.loc[df.index[i],'st_line']=df['final_upperband'].iloc[i]
            df.loc[df.index[i],'st_dir']=1
    df['ema_val']=df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df

async def check_paper_trades(client, df_live, df_closed, symbol):
    global BALANCE_DATA
    try:
        async with _lock:
            if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!='OPEN': return
            trade = PAPER_TRADES[symbol].copy()
        clow_live=float(df_live['low'].iloc[-1]); chigh_live=float(df_live['high'].iloc[-1])
        entry=trade['entry']; attempt=trade.get('attempt',1)
        if clow_live <= trade['tp']:
            amt=trade.get('trade_amount_usdt', trade['balance_at_entry']*POSITION_SIZE_PERCENT)
            if attempt==1 and not trade.get('tp1_hit'):
                partial=amt*0.5; gpct=((entry-trade['tp'])/entry)*100; gusdt=partial*gpct/100
                fee=partial*EFFECTIVE_FEE_RATE + (partial+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
                async with _lock:
                    if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status']=='OPEN':
                        BALANCE_DATA['total_balance']+=nusdt; BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
                        BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
                        PAPER_TRADES[symbol]['tp1_hit']=True; PAPER_TRADES[symbol]['sl']=entry; PAPER_TRADES[symbol]['max_favorable_pnl_pct']=5.0
                await save_balance_data(client); await save_paper_trades(client)
                asyncio.create_task(send_telegram(client, f"🎯 <b>50% TP1 BOOKED -5%</b> {symbol} #{attempt}/3 SL->BE"))
                return
            else:
                gpct=((entry-trade['tp'])/entry)*100; gusdt=amt*gpct/100; fee=amt*EFFECTIVE_FEE_RATE + (amt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
                npct=(nusdt/amt)*100 if amt>0 else 0
                async with _lock:
                    BALANCE_DATA['total_balance']+=nusdt; BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
                    BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
                    PAPER_TRADES[symbol]['status']='CLOSED_TP'; PAPER_TRADES[symbol]['pnl_percent']=round(npct,2); PAPER_TRADES[symbol]['pnl_usdt']=round(nusdt,2)
                    WATCHLIST.pop(symbol,None)
                await save_balance_data(client); await save_paper_trades(client); await save_watchlist(client)
                asyncio.create_task(send_telegram(client, f"✅ <b>100% TP HIT #{attempt}</b> {symbol} 🗑️"))
                return
        if trade.get('tp1_hit') and attempt==1:
            max_drop=((entry-clow_live)/entry)*100; prev_max=trade.get('max_favorable_pnl_pct',5.0)
            if max_drop>prev_max:
                steps=math.floor(max_drop-5.0)
                if steps>math.floor(prev_max-5.0):
                    locked=steps*1.0; new_sl=entry*(1-locked/100.0)
                    new_sl=price_to_tick(new_sl, await get_tick_size(client, symbol) or 0.0001)
                    if new_sl<trade['sl']:
                        async with _lock:
                            if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status']=='OPEN':
                                PAPER_TRADES[symbol]['sl']=new_sl; PAPER_TRADES[symbol]['max_favorable_pnl_pct']=max_drop
                        await save_paper_trades(client)
        if chigh_live >= trade['sl']:
            amt=trade.get('trade_amount_usdt', trade['balance_at_entry']*POSITION_SIZE_PERCENT)
            ratio=0.5 if (attempt==1 and trade.get('tp1_hit')) else 1.0; tamt=amt*ratio
            gpct=((entry-trade['sl'])/entry)*100; gusdt=tamt*gpct/100
            fee=tamt*EFFECTIVE_FEE_RATE + max(0,tamt+gusdt)*EFFECTIVE_FEE_RATE; nusdt=gusdt-fee
            async with _lock:
                BALANCE_DATA['total_balance']+=nusdt; BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
                BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
                PAPER_TRADES[symbol]['status']='CLOSED_SL'; PAPER_TRADES[symbol]['pnl_percent']=round((nusdt/tamt)*100,2) if tamt>0 else 0; PAPER_TRADES[symbol]['pnl_usdt']=round(nusdt,2)
                if attempt==1:
                    if trade.get('tp1_hit'): WATCHLIST.pop(symbol,None); rmsg=f"❌ <b>SL BE HIT</b> {symbol} #{attempt}/3 🗑️"
                    else: WATCHLIST[symbol]['attempts']=1; WATCHLIST[symbol]['last_state']='reset'; WATCHLIST[symbol]['trigger_low']=None; rmsg=f"❌ <b>SL HIT</b> {symbol} #{attempt}/3 ⏳ Next #2"
                elif attempt==2:
                    WATCHLIST[symbol]['attempts']=2; WATCHLIST[symbol]['last_state']='wait_above_st'; WATCHLIST[symbol]['trigger_low']=None;
                    rmsg=f"❌ <b>SL HIT</b> {symbol} #{attempt}/3 ⏳ Waiting ST above then break for #3"
                else:
                    WATCHLIST.pop(symbol,None);
                    cooldown_coins[symbol] = time.time() + 3*3600
                    rmsg=f"❌ <b>SL HIT</b> {symbol} #{attempt}/3 🗑️ END - 3hr cooldown"
            await save_balance_data(client); await save_paper_trades(client); await save_watchlist(client)
            asyncio.create_task(send_telegram(client, rmsg))
    except Exception as e: print(f"check trades error {symbol}: {e}", flush=True)

async def bot1_scan(client):
    print("Bot1: Started v8.7.4", flush=True)
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
                                if time.time() < cooldown_coins[s]:
                                    continue
                                else:
                                    del cooldown_coins[s]
                            WATCHLIST[s]={'time':time.time(),'attempts':0,'last_state':'reset','trigger_low':None}; added+=1
                            asyncio.create_task(send_telegram(client, f"🚨 <b>40%+ PUMP</b> {s} +{ch:.2f}%"))
            if added>0: await save_watchlist(client)
        except Exception as e: print(f"Bot1 Error: {e}", flush=True)
        await asyncio.sleep(60)

async def process_symbol(client, symbol):
    try:
        df_live_raw=await get_klines(client, symbol, include_current=True, limit=402)
        if df_live_raw is None or len(df_live_raw) < EMA_PERIOD+2: return False
        df_closed_raw=df_live_raw.iloc[:-1].reset_index(drop=True)
        df_live=await asyncio.to_thread(calculate_supertrend, df_live_raw, ATR_PERIOD, ATR_MULTIPLIER)
        df_closed=await asyncio.to_thread(calculate_supertrend, df_closed_raw, ATR_PERIOD, ATR_MULTIPLIER)
        await check_paper_trades(client, df_live, df_closed, symbol)

        close_live=float(df_live['close'].iloc[-1])
        low_live=float(df_live['low'].iloc[-1])
        ema_live=float(df_live['ema_val'].iloc[-1])
        low_closed=float(df_closed['low'].iloc[-1])
        close_closed=float(df_closed['close'].iloc[-1]); prev_close_closed=float(df_closed['close'].iloc[-2])
        ema_closed=float(df_closed['ema_val'].iloc[-1]); prev_ema_closed=float(df_closed['ema_val'].iloc[-2])
        st_closed=float(df_closed['st_line'].iloc[-1]); prev_st_closed=float(df_closed['st_line'].iloc[-2])
        st_dir_closed=int(df_closed['st_dir'].iloc[-1])

        changed=False; new=False; msg=None
        tick=await get_tick_size(client, symbol)
        if tick is None: return False

        # Live price fetch outside lock (to avoid await inside lock)
        live_price_for_check = await get_live_price(client, symbol)

        async with _lock:
            if symbol not in WATCHLIST: return False
            att=WATCHLIST[symbol].get('attempts',0)
            open_exists=PAPER_TRADES.get(symbol) and PAPER_TRADES[symbol].get('status')=='OPEN'
            active=sum(1 for t in PAPER_TRADES.values() if t.get('status')=='OPEN')
            should=False; exec_price=None; trig_for_msg=None

            # ===== V8.7.4 FIXED LOGIC - EMA SE 2 PIP NEECHE =====
            if att==0:
                if not open_exists:
                    live_price = live_price_for_check if live_price_for_check is not None else close_live
                    ema_300_live = ema_live
                    if live_price < ema_300_live:
                        should=True
                        trig_for_msg=ema_300_live
                        ema_break_price = ema_300_live - (tick * PIP_SIZE)
                        exec_price=price_to_tick(ema_break_price, tick)

            elif att==1:
                if WATCHLIST[symbol].get('trigger_low') is None:
                    is_cross = st_closed < ema_closed and prev_st_closed >= prev_ema_closed and st_dir_closed == 1
                    if is_cross:
                        WATCHLIST[symbol]['trigger_low']=low_closed
                        WATCHLIST[symbol]['last_state']='waiting_break_2'
                        changed=True
                        asyncio.create_task(send_telegram(client, f"📌 <b>2nd Trigger Marked</b> {symbol} Low ${low_closed:.8f} - Waiting break x{PIP_SIZE} [RED ST]"))
                else:
                    trig=WATCHLIST[symbol]['trigger_low']
                    if low_live <= trig - (tick * PIP_SIZE):
                        should=True; trig_for_msg=trig; exec_price=price_to_tick(trig - (tick * PIP_SIZE), tick)

            elif att==2:
                state=WATCHLIST[symbol].get('last_state','wait_above_st')
                if state=='wait_above_st' and close_closed > st_closed and st_dir_closed == 1:
                    WATCHLIST[symbol]['last_state']='ready_for_st_cross'; WATCHLIST[symbol]['trigger_low']=None; changed=True
                elif state=='ready_for_st_cross' and WATCHLIST[symbol].get('trigger_low') is None:
                    is_cross = close_closed < st_closed and prev_close_closed >= prev_st_closed and st_dir_closed == 1
                    if is_cross:
                        WATCHLIST[symbol]['trigger_low']=low_closed
                        WATCHLIST[symbol]['last_state']='waiting_break_3'
                        changed=True
                        asyncio.create_task(send_telegram(client, f"📌 <b>3rd Trigger Marked</b> {symbol} Low ${low_closed:.8f} - Waiting break [RED ST BREAK]"))
                elif WATCHLIST[symbol].get('last_state')=='waiting_break_3' and WATCHLIST[symbol].get('trigger_low') is not None:
                    trig=WATCHLIST[symbol]['trigger_low']
                    if low_live <= trig - (tick * PIP_SIZE):
                        should=True; trig_for_msg=trig; exec_price=price_to_tick(trig - (tick * PIP_SIZE), tick)

            if should and att<3 and not open_exists and active<MAX_OPEN_TRADES:
                ep=price_to_tick(exec_price, tick); tp=price_to_tick(ep*(1-TARGET_TP_PERCENT), tick); sl=price_to_tick(ep*(1+EMERGENCY_SL_PERCENT), tick)
                tamt=BALANCE_DATA['total_balance']*POSITION_SIZE_PERCENT; cur=att+1
                WATCHLIST[symbol]['attempts']=cur; WATCHLIST[symbol]['last_state']='short'; WATCHLIST[symbol]['trigger_low']=None
                PAPER_TRADES[symbol]={'entry':ep,'tp':tp,'sl':sl,'status':'OPEN','time':time.time(),'balance_at_entry':BALANCE_DATA['total_balance'],'trade_amount_usdt':tamt,'attempt':cur,'max_favorable_pnl_pct':0.0,'tp1_hit':False}
                if cur==1:
                    msg=f"⚡ <b>FAST SHORT #1 LIVE</b> {symbol} #{cur}/3\nEntry ${ep:.8f} (EMA300 {PIP_SIZE} pip below)\nEMA300 ${trig_for_msg:.8f}\nTP ${tp:.8f} (-5%)\nSL ${sl:.8f} (+2%)"
                else:
                    msg=f"📝 <b>SHORT BREAK ENTRY</b> {symbol} #{cur}/3\nEntry ${ep:.8f}\nTP ${tp:.8f} (-5%)\nSL ${sl:.8f} (+2%)\nTrig Low ${trig_for_msg:.8f} break x{PIP_SIZE}"
                new=True; changed=True
            if time.time()-WATCHLIST[symbol]['time'] > WATCHLIST_DAYS*86400 and not (PAPER_TRADES.get(symbol,{}).get('status')=='OPEN'): WATCHLIST.pop(symbol,None); changed=True
        if new:
            await save_paper_trades(client)
            asyncio.create_task(send_telegram(client, msg))
        return changed
    except Exception as e: print(f"process_symbol error {symbol}: {e}", flush=True); traceback.print_exc(); return False

async def bot2_scan(client):
    print("Bot2: Started v8.7.4", flush=True)
    while True:
        try:
            async with _lock: syms=list(WATCHLIST.keys())
            if not syms: await asyncio.sleep(10); continue
            results=await asyncio.gather(*[process_symbol(client, s) for s in syms])
            if any(results): await save_watchlist(client)
        except Exception as e: print(f"Bot2 Error: {e}", flush=True)
        await asyncio.sleep(5)

@app.route('/')
def home(): return jsonify({"status":"v8.7.4 EMA 2 PIP BELOW FIX","watchlist":len(WATCHLIST),"cooldown":len(cooldown_coins)})

@app.route('/webhook', methods=['POST'])
def webhook():
    global application, main_event_loop
    try:
        if application is None or main_event_loop is None:
            return jsonify({"ok": True}), 200
        data = request.get_json(force=True, silent=True)
        if not data: return jsonify({"ok": True}), 200
        update = Update.de_json(data, application.bot)
        asyncio.run_coroutine_threadsafe(application.process_update(update), main_event_loop)
    except Exception as e:
        print(f"Webhook error: {e}", flush=True)
    return jsonify({"ok": True}), 200

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
        for cmd, fn in [("start", start_command), ("add", add_command), ("remove", remove_command), ("watchlist", watchlist_command), ("open", open_command), ("close", close_command), ("pnl", pnl_command)]:
            app_t.add_handler(CommandHandler(cmd, fn))
        await app_t.initialize()
        await app_t.start()
        if WEBHOOK_URL:
            wh_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            try:
                await app_t.bot.delete_webhook(drop_pending_updates=True)
                await asyncio.sleep(1)
                await app_t.bot.set_webhook(url=wh_url, drop_pending_updates=True)
                print(f"✅ WEBHOOK SET: {wh_url}", flush=True)
            except Exception as e: print(f"Webhook set error: {e}", flush=True)
        else:
            try: await app_t.bot.delete_webhook(drop_pending_updates=True)
            except: pass
            await asyncio.sleep(5)
            await app_t.updater.start_polling(drop_pending_updates=True, poll_interval=2.0, bootstrap_retries=-1)
        port = int(os.environ.get("PORT", 10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
        asyncio.create_task(bot1_scan(client))
        asyncio.create_task(bot2_scan(client))
        print("v8.7.4 Operational", flush=True)
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