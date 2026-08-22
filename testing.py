import threading, asyncio, httpx, time, os, json, pandas as pd, math, logging
from flask import Flask, jsonify
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# ===== CONFIG =====
PUMP_PERCENT_24H = 40
WATCHLIST_DAYS = 2
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
RISK_PER_TRADE = 0.20
MAX_OPEN_TRADES = 4
MIN_VOLUME_24H = 2000000
EMERGENCY_SL_PERCENT = 0.020
TARGET_TP_PERCENT = 0.050
TAKER_FEE = 0.0005
GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)

GIST_ID = os.environ.get("GIST_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None

WATCHLIST = {}
PAPER_TRADES = {}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")
_lock = asyncio.Lock()
BALANCE_DATA = {"total_balance": 10000.0, "starting_balance": 10000.0, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}

def get_coindcx_pair(s): return f"F-{s.replace('USDT','').replace('.P','')}_USDT"

async def gist_get(client, filename):
    if not GIST_URL or not GITHUB_TOKEN: return {}
    try:
        r = await client.get(GIST_URL, headers=GIST_HEADERS, timeout=10.0)
        if r.status_code!=200: return {}
        d=r.json()
        if filename in d.get('files',{}):
            c=d['files'][filename]['content']
            return json.loads(c) if c else {}
    except: pass
    return {}
async def gist_set(client, filename, content):
    if not GIST_URL or not GITHUB_TOKEN: return False
    payload={"files":{filename:{"content": json.dumps(content, indent=2)}}}
    for _ in range(3):
        try:
            r=await client.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=15.0)
            if r.status_code==200: return True
        except: await asyncio.sleep(2)
    return False
async def save_watchlist(c): await gist_set(c, 'watchlist.json', {'coins': WATCHLIST})
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
async def save_paper_trades(c): await gist_set(c, 'paper_trades.json', PAPER_TRADES)
async def load_paper_trades(c):
    global PAPER_TRADES
    PAPER_TRADES=await gist_get(c, 'paper_trades.json') or {}
async def save_balance_data(c): await gist_set(c, 'total_pnl.json', BALANCE_DATA)
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

async def start_command(u,c): await u.message.reply_text("✅ Bot v5.9 FINAL - Complete Rules")
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
        async with _lock: WATCHLIST.pop(s,None)
        cl=c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"🗑️ {s} removed", parse_mode="HTML")
async def watchlist_command(u,c):
    coins=", ".join(WATCHLIST.keys()) if WATCHLIST else "Empty"
    await u.message.reply_text(f"📋 Watchlist ({len(WATCHLIST)}):\n{coins}", parse_mode="HTML")
async def open_command(u,c):
    o={k:v for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'}
    if not o: return await u.message.reply_text("No Open Trades", parse_mode="HTML")
    msg=f"📊 OPEN ({len(o)}/{MAX_OPEN_TRADES})\n\n"
    for s,t in o.items(): msg+=f"{s} #{t.get('attempt',1)}/3 Entry ${t['entry']:.6f} TP ${t['tp']:.6f} SL ${t['sl']:.6f} {'🎯50% Booked' if t.get('tp1_hit') else ''}\n\n"
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
        amt=tr.get('trade_amount_usdt', tr['balance_at_entry']*RISK_PER_TRADE)*ratio
        gpct=((tr['entry']-ep)/tr['entry'])*100
        gusdt=amt*gpct/100
        fee=amt*EFFECTIVE_FEE_RATE + max(0,amt+gusdt)*EFFECTIVE_FEE_RATE
        nusdt=gusdt-fee
        BALANCE_DATA['total_balance']+=nusdt
        PAPER_TRADES[s]['status']='CLOSED_MANUAL'
        WATCHLIST.pop(s,None)
        if cl:
            await save_paper_trades(cl); await save_balance_data(cl); await save_watchlist(cl)
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

async def get_klines_coindcx_async(client, symbol, interval='5m', limit=400, include_current=False):
    pair=get_coindcx_pair(symbol)
    url="https://api.coindcx.com/exchange/v1/candles"
    params={'pair':pair,'interval':interval,'limit':limit}
    try:
        res=await client.get(url, params=params, timeout=10.0)
        data=res.json()
        if not data or not isinstance(data, list): return None
        df=pd.DataFrame(data).rename(columns={'time':'timestamp'})
        df['timestamp']=df['timestamp'].astype('int64')
        df[['open','high','low','close']]=df[['open','high','low','close']].astype(float)
        df=df[['timestamp','open','high','low','close']].sort_values('timestamp').reset_index(drop=True)
        if not include_current: df=df.iloc[:-1].reset_index(drop=True)
        if len(df)>=50: return df
    except: pass
    return None

async def get_klines(client, symbol, interval='5', limit=400, include_current=False):
    c_interval='5m' if interval=='5' else interval
    df=await get_klines_bybit_async(client, symbol, interval, limit, include_current)
    if df is not None: return df
    return await get_klines_coindcx_async(client, symbol, c_interval, limit, include_current)

def calculate_supertrend(df, period=10, multiplier=3):
    df=df.copy()
    df['h-l']=df['high']-df['low']
    df['h-pc']=abs(df['high']-df['close'].shift(1))
    df['l-pc']=abs(df['low']-df['close'].shift(1))
    df['tr']=df[['h-l','h-pc','l-pc']].max(axis=1)
    df['atr']=df['tr'].ewm(alpha=1/period, adjust=False).mean()
    hl2=(df['high']+df['low'])/2
    df['upperband']=hl2+(multiplier*df['atr'])
    df['lowerband']=hl2-(multiplier*df['atr'])
    df['final_upperband']=0.0; df['final_lowerband']=0.0; df['supertrend']=True; df['st_line']=0.0
    for i in range(len(df)):
        if i==0:
            df.loc[df.index[i],'final_upperband']=df['upperband'].iloc[i]
            df.loc[df.index[i],'final_lowerband']=df['lowerband'].iloc[i]
            df.loc[df.index[i],'st_line']=df['upperband'].iloc[i]
            continue
        if df['upperband'].iloc[i] < df['final_upperband'].iloc[i-1] or df['close'].iloc[i-1] > df['final_upperband'].iloc[i-1]:
            df.loc[df.index[i],'final_upperband']=df['upperband'].iloc[i]
        else: df.loc[df.index[i],'final_upperband']=df['final_upperband'].iloc[i-1]
        if df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i-1] or df['close'].iloc[i-1] < df['final_lowerband'].iloc[i-1]:
            df.loc[df.index[i],'final_lowerband']=df['lowerband'].iloc[i]
        else: df.loc[df.index[i],'final_lowerband']=df['final_lowerband'].iloc[i-1]
        prev_st=df['supertrend'].iloc[i-1]; close_i=df['close'].iloc[i]
        if prev_st and close_i < df['final_lowerband'].iloc[i]: df.loc[df.index[i],'supertrend']=False
        elif not prev_st and close_i > df['final_upperband'].iloc[i]: df.loc[df.index[i],'supertrend']=True
        else: df.loc[df.index[i],'supertrend']=prev_st
        df.loc[df.index[i],'st_line']=df['final_lowerband'].iloc[i] if df['supertrend'].iloc[i] else df['final_upperband'].iloc[i]
    df['ema_val']=df['close'].ewm(span=EMA_PERIOD, adjust=False).mean().rolling(window=9, min_periods=1).mean()
    return df

async def check_paper_trades(client, df_live, symbol):
    global BALANCE_DATA
    async with _lock:
        if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!='OPEN': return
        trade = PAPER_TRADES[symbol].copy()
    clow=df_live['low'].iloc[-1]; chigh=df_live['high'].iloc[-1]; cclose=df_live['close'].iloc[-1]; st=df_live['st_line'].iloc[-1]
    entry=trade['entry']; attempt=trade.get('attempt',1)

    if clow <= trade['tp'] and not trade.get('tp1_hit'):
        trade_amount = trade.get('trade_amount_usdt', trade['balance_at_entry']*RISK_PER_TRADE)
        if attempt == 1:
            partial=trade_amount*0.5
            gross_pct=((entry-trade['tp'])/entry)*100
            gross_usdt=partial*gross_pct/100
            fee=partial*EFFECTIVE_FEE_RATE + (partial+gross_usdt)*EFFECTIVE_FEE_RATE
            net_usdt=gross_usdt-fee
            async with _lock:
                if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status']=='OPEN':
                    BALANCE_DATA['total_balance']+=net_usdt
                    PAPER_TRADES[symbol]['tp1_hit']=True
                    PAPER_TRADES[symbol]['sl']=entry
                    PAPER_TRADES[symbol]['max_favorable_pnl_pct']=5.0
                    trade['tp1_hit']=True; trade['sl']=entry; trade['max_favorable_pnl_pct']=5.0
            await save_balance_data(client); await save_paper_trades(client)
            asyncio.create_task(send_telegram(client, f"🎯 <b>50% TP1 BOOKED (-5%) - FIRST ENTRY</b>\n<b>{symbol}</b> #{attempt}/3\nNet: ${net_usdt:.2f}\nSL -> BE ${entry:.6f}"))
        else:
            gross_pct=((entry-trade['tp'])/entry)*100
            gross_usdt=trade_amount*gross_pct/100
            fee=trade_amount*EFFECTIVE_FEE_RATE + (trade_amount+gross_usdt)*EFFECTIVE_FEE_RATE
            net_usdt=gross_usdt-fee
            net_pct=(net_usdt/trade_amount)*100
            async with _lock:
                if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!='OPEN': return
                BALANCE_DATA['total_balance']+=net_usdt
                BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
                BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
                PAPER_TRADES[symbol]['status']='CLOSED_TP'
                PAPER_TRADES[symbol]['pnl_percent']=round(net_pct,2)
                PAPER_TRADES[symbol]['pnl_usdt']=round(net_usdt,2)
                WATCHLIST.pop(symbol,None)
            await save_balance_data(client); await save_paper_trades(client); await save_watchlist(client)
            asyncio.create_task(send_telegram(client, f"✅ <b>100% TP HIT - {attempt} ENTRY CLOSED</b>\n<b>{symbol}</b> #{attempt}/3\nNet: {net_pct:.2f}% / ${net_usdt:.2f}\n🗑️ Removed"))
            return

    if trade.get('tp1_hit') and attempt==1:
        max_drop=((entry-clow)/entry)*100
        prev_max=trade.get('max_favorable_pnl_pct',5.0)
        if max_drop>prev_max:
            steps=math.floor(max_drop-5.0)
            if steps>math.floor(prev_max-5.0):
                locked=steps*1.0
                new_sl=round(entry*(1-locked/100.0),6)
                if new_sl<trade['sl']:
                    async with _lock:
                        if symbol in PAPER_TRADES and PAPER_TRADES[symbol]['status']=='OPEN':
                            PAPER_TRADES[symbol]['sl']=new_sl
                            PAPER_TRADES[symbol]['max_favorable_pnl_pct']=max_drop
                            trade['sl']=new_sl
                    await save_paper_trades(client)

    sl_hit=chigh >= trade['sl']
    st_exit=trade.get('tp1_hit',False) and attempt==1 and (cclose > st)

    if sl_hit or st_exit:
        tp1_done=trade.get('tp1_hit',False)
        ratio=0.5 if tp1_done else 1.0
        if sl_hit:
            eprice=trade['sl']; scode='CLOSED_SL'; emoji='❌' if not tp1_done else '🛡️'
            rtxt="Hard SL 2%" if not tp1_done else "Trailing SL Hit"
        else:
            eprice=cclose; scode='CLOSED_ST_EXIT'; emoji='🚀'
            rtxt="ST Reversal - Runner Closed"

        # ===== FINAL FIX - TERA NAYA REMOVE LOGIC =====
        if attempt == 1:
            if tp1_done:
                # First entry me TP1 ke baad jo bhi exit ho (Trailing SL ya ST Reversal) -> REMOVE
                remove_from_watchlist = True
            else:
                # TP1 se pehle SL hit -> REMOVE MAT KARO, second entry ka wait karo
                remove_from_watchlist = False
        else:
            # Second / Third entry me koi bhi exit ho (SL ya 100% TP) -> REMOVE
            remove_from_watchlist = True

        async with _lock:
            if symbol not in PAPER_TRADES or PAPER_TRADES[symbol]['status']!='OPEN': return
            tamt=trade.get('trade_amount_usdt', trade['balance_at_entry']*RISK_PER_TRADE)*ratio
            gpct=((entry-eprice)/entry)*100
            gusdt=tamt*gpct/100
            fee=tamt*EFFECTIVE_FEE_RATE + max(0,tamt+gusdt)*EFFECTIVE_FEE_RATE
            nusdt=gusdt-fee
            npct=(nusdt/tamt)*100 if tamt>0 else 0
            BALANCE_DATA['total_balance']+=nusdt
            BALANCE_DATA['lifetime_pnl_usdt']=BALANCE_DATA['total_balance']-BALANCE_DATA['starting_balance']
            BALANCE_DATA['lifetime_pnl_percent']=(BALANCE_DATA['lifetime_pnl_usdt']/BALANCE_DATA['starting_balance'])*100
            PAPER_TRADES[symbol]['status']=scode
            PAPER_TRADES[symbol]['pnl_percent']=round(npct,2)
            PAPER_TRADES[symbol]['pnl_usdt']=round(nusdt,2)
            if remove_from_watchlist: WATCHLIST.pop(symbol,None)
            else:
                if symbol in WATCHLIST: WATCHLIST[symbol]['last_state']='reset'
        await save_balance_data(client); await save_paper_trades(client); await save_watchlist(client)
        msg=f"{emoji} <b>TRADE CLOSED</b>\n<b>{symbol}</b> #{attempt}/3\n{rtxt}\nExit ${eprice:.6f}\nPnL {npct:.2f}% / ${nusdt:.2f}"
        if remove_from_watchlist: msg+="\n🗑️ Removed from watchlist (First Entry Complete)"
        else: msg+="\n⏳ Waiting for 2nd Entry (ST Reset Needed)"
        asyncio.create_task(send_telegram(client, msg))

async def bot1_scan(client):
    while True:
        try:
            url="https://api.bybit.com/v5/market/tickers?category=linear"
            res=await client.get(url, timeout=20.0)
            data=res.json()
            added=0
            if data.get('retCode')==0 and data.get('result'):
                for t in data['result']['list']:
                    m=t.get('symbol','')
                    if not m.endswith('USDT') and not m.endswith('USDT.P'): continue
                    s=m.replace('.P','')
                    try:
                        ch=float(t.get('price24hPcnt',0))*100; vol=float(t.get('volume24h',0)); lp=float(t.get('lastPrice',0))
                    except: continue
                    if vol < MIN_VOLUME_24H or lp < 0.001: continue
                    async with _lock:
                        if ch >= PUMP_PERCENT_24H and s not in WATCHLIST:
                            WATCHLIST[s]={'time':time.time(),'attempts':0,'last_state':'reset','trigger_low':None}
                            added+=1
                            asyncio.create_task(send_telegram(client, f"🚨 <b>40% PUMP</b> {s} +{ch:.2f}%"))
            if added>0: await save_watchlist(client)
        except Exception as e: print(f"Bot1 Error {e}", flush=True)
        await asyncio.sleep(60)

async def process_symbol(client, symbol):
    df_live=await get_klines(client, symbol, include_current=True)
    if df_live is None or len(df_live) < EMA_PERIOD+2: return False
    df_live=calculate_supertrend(df_live, ATR_PERIOD, ATR_MULTIPLIER)
    df_closed=await get_klines(client, symbol, include_current=False)
    if df_closed is None or len(df_closed) < EMA_PERIOD+2: return False
    df_closed=calculate_supertrend(df_closed, ATR_PERIOD, ATR_MULTIPLIER)
    await check_paper_trades(client, df_live, symbol)

    ema_c=df_closed['ema_val'].iloc[-1]; close_c=df_closed['close'].iloc[-1]; low_c=df_closed['low'].iloc[-1]
    prev_close=df_closed['close'].iloc[-2]
    clow_live=df_live['low'].iloc[-1]; close_live=df_live['close'].iloc[-1]; ema_live=df_live['ema_val'].iloc[-1]; st_live=df_live['st_line'].iloc[-1]
    changed=False; msg=None; new=False
    async with _lock:
        if symbol not in WATCHLIST: return False
        att=WATCHLIST[symbol].get('attempts',0)
        trig=WATCHLIST[symbol].get('trigger_low',None)
        open_exists=PAPER_TRADES.get(symbol) and PAPER_TRADES[symbol].get('status')=='OPEN'
        active=sum(1 for t in PAPER_TRADES.values() if t.get('status')=='OPEN')
        should=False; exec_price=None
        if att==0:
            is_cross=close_c < ema_c and prev_close >= ema_c
            if trig is None and is_cross:
                WATCHLIST[symbol]['trigger_low']=low_c
                changed=True
            elif trig is not None:
                if clow_live < trig:
                    should=True
                    pip=0.01 if trig>=100 else (0.0001 if trig>=1 else 0.000001)
                    exec_price=round(trig-pip,6)
        elif att in [1,2]:
            if close_live > st_live:
                WATCHLIST[symbol]['last_state']='reset'
                changed=True
            if close_live < st_live and st_live < ema_live and WATCHLIST[symbol].get('last_state')=='reset':
                should=True
                exec_price=close_live
        if should and att<3 and not open_exists and active<MAX_OPEN_TRADES:
            ep=round(exec_price,6); tp=round(ep*(1-TARGET_TP_PERCENT),6); sl=round(ep*(1+EMERGENCY_SL_PERCENT),6)
            tamt=BALANCE_DATA['total_balance']*RISK_PER_TRADE
            cur=att+1
            WATCHLIST[symbol]['attempts']=cur; WATCHLIST[symbol]['last_state']='short'; WATCHLIST[symbol]['trigger_low']=None
            PAPER_TRADES[symbol]={'entry':ep,'tp':tp,'sl':sl,'status':'OPEN','time':time.time(),'balance_at_entry':BALANCE_DATA['total_balance'],'trade_amount_usdt':tamt,'attempt':cur,'max_favorable_pnl_pct':0.0,'tp1_hit':False}
            msg=f"📝 <b>SHORT ENTRY</b> {symbol} #{cur}/3\nEntry ${ep:.6f}\nTP ${tp:.6f} (-5%)\nSL ${sl:.6f} (+2%)"
            new=True; changed=True
        has_open=PAPER_TRADES.get(symbol,{}).get('status')=='OPEN'
        if time.time()-WATCHLIST[symbol]['time'] > WATCHLIST_DAYS*86400 and not has_open:
            WATCHLIST.pop(symbol,None); changed=True
    if new:
        await save_paper_trades(client)
        asyncio.create_task(send_telegram(client, msg))
    return changed

async def bot2_scan(client):
    while True:
        try:
            async with _lock: syms=list(WATCHLIST.keys())
            if not syms: await asyncio.sleep(20); continue
            results=await asyncio.gather(*[process_symbol(client, s) for s in syms])
            if any(results): await save_watchlist(client)
        except Exception as e: print(f"Bot2 {e}", flush=True)
        await asyncio.sleep(10)

@app.route('/')
def home(): return jsonify({"status":"v5.9 FINAL - Complete Remove Logic","watchlist":len(WATCHLIST)})

async def main_async():
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
    async with httpx.AsyncClient(limits=limits) as client:
        await load_watchlist(client); await load_paper_trades(client); await load_balance_data(client)
        t_req=HTTPXRequest(connection_pool_size=20, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
        app_t=ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
        app_t.bot_data["http_client"]=client
        for cmd,fn in [("start",start_command),("add",add_command),("remove",remove_command),("watchlist",watchlist_command),("open",open_command),("close",close_command),("pnl",pnl_command)]:
            app_t.add_handler(CommandHandler(cmd,fn))
        await app_t.bot.delete_webhook(drop_pending_updates=True)
        port=int(os.environ.get("PORT",10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
        asyncio.create_task(bot1_scan(client)); asyncio.create_task(bot2_scan(client))
        await app_t.initialize(); await app_t.start(); await app_t.updater.start_polling(drop_pending_updates=True)
        print("v5.9 Operational - FINAL", flush=True)
        while True: await asyncio.sleep(3600)

def main():
    loop=asyncio.get_event_loop(); loop.run_until_complete(main_async())
if __name__=='__main__': main()
