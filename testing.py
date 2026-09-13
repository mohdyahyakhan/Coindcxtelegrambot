# COINDEX V8.8.4 - SEPARATE PNL - Crypto + NSE Split
import threading, asyncio, httpx, time, os, json, pandas as pd, numpy as np, math, logging, pytz
from decimal import Decimal, ROUND_DOWN
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
from datetime import datetime, timezone

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

# --- CONFIG SAME ---
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

# --- ENV ---
GIST_ID = os.environ.get("GIST_ID")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_BOT_TOKEN = BOT_TOKEN
TELEGRAM_CHAT_ID = CHAT_ID
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")

# --- SEPARATE STORAGE ---
WATCHLIST = {}
PAPER_TRADES = {} # Crypto
PAPER_TRADES_NSE = {} # NSE
TICK_CACHE = {}
cooldown_coins = {}
_lock = asyncio.Lock(); _gist_lock = asyncio.Lock()

# === NEW - SEPARATE BALANCE ===
BALANCE_CRYPTO = {"total_balance": 10000.0, "starting_balance": 10000.0, "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
BALANCE_NSE = {"total_balance": 100000.0, "starting_balance": 100000.0, "lifetime_pnl": 0.0, "lifetime_pnl_percent": 0.0}

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
async def save_paper_trades_crypto(c):
    async with _lock: snapshot = dict(PAPER_TRADES)
    await gist_set_locked(c, 'paper_trades_crypto.json', snapshot)
async def save_balance_crypto(c):
    async with _lock: snapshot = dict(BALANCE_CRYPTO)
    await gist_set_locked(c, 'total_pnl_crypto.json', snapshot)
async def save_paper_trades_nse(c):
    async with _lock: snapshot = dict(PAPER_TRADES_NSE)
    await gist_set_locked(c, 'paper_trades_nse.json', snapshot)
async def save_balance_nse(c):
    async with _lock: snapshot = dict(BALANCE_NSE)
    await gist_set_locked(c, 'total_pnl_nse.json', snapshot)

async def load_all(c):
    global WATCHLIST, PAPER_TRADES, PAPER_TRADES_NSE, BALANCE_CRYPTO, BALANCE_NSE
    # Watchlist
    data=await gist_get(c, 'watchlist.json')
    if data and 'coins' in data:
        WATCHLIST = {k.replace('.P',''):v for k,v in data['coins'].items() if isinstance(v,dict)}
    # Crypto trades
    data = await gist_get(c, 'paper_trades_crypto.json') or await gist_get(c, 'paper_trades.json')
    if data: PAPER_TRADES = data
    # NSE trades
    data = await gist_get(c, 'paper_trades_nse.json')
    if data: PAPER_TRADES_NSE = data
    # Crypto balance
    d=await gist_get(c, 'total_pnl_crypto.json') or await gist_get(c, 'total_pnl.json')
    if d and 'total_balance' in d: BALANCE_CRYPTO = d
    # NSE balance
    d=await gist_get(c, 'total_pnl_nse.json')
    if d and 'total_balance' in d: BALANCE_NSE = d
    # Migration for old file
    if not await gist_get(c, 'total_pnl_crypto.json') and await gist_get(c, 'total_pnl.json'):
        await save_balance_crypto(c)

async def send_telegram(client, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"HTML"}
    try: await client.post(url, json=payload, timeout=10.0)
    except: pass

# --- COMMANDS ---
async def start_command(u,c):
    await u.message.reply_text("✅ Bot v8.8.4 SEPARATE PNL\nCrypto $10k | NSE ₹1L\n/pnlcrypto /pnlnse", parse_mode="HTML")

async def pnl_command(u,c):
    async with _lock:
        bc=dict(BALANCE_CRYPTO); bn=dict(BALANCE_NSE)
        oc=len([t for t in PAPER_TRADES.values() if t.get('status')=='OPEN'])
        on=len([t for t in PAPER_TRADES_NSE.values() if t.get('status')=='OPEN'])
    msg = f"📊 <b>PNL V8.8.4 SEPARATE</b>\n\n"
    msg+= f"<b>CRYPTO (BOT1+2):</b>\nBal: ${bc['total_balance']:.2f} | PnL: {bc['lifetime_pnl_percent']:.2f}% (${bc['lifetime_pnl_usdt']:.2f})\nOpen: {oc}/{MAX_OPEN_TRADES}\n\n"
    msg+= f"<b>NSE (BOT3):</b>\nBal: ₹{bn['total_balance']:.2f} | PnL: {bn['lifetime_pnl_percent']:.2f}% (₹{bn['lifetime_pnl']:.2f})\nOpen: {on}\n"
    await u.message.reply_text(msg, parse_mode="HTML")

async def pnlcrypto_command(u,c):
    async with _lock: b=dict(BALANCE_CRYPTO); o={k:v for k,v in PAPER_TRADES.items() if v.get('status')=='OPEN'}
    msg=f"💰 <b>CRYPTO PNL (BOT1+BOT2)</b>\nBal: ${b['total_balance']:.2f}\nStart: ${b['starting_balance']:.2f}\nPnL: {b['lifetime_pnl_percent']:.2f}% (${b['lifetime_pnl_usdt']:.2f})\nOpen: {len(o)}\n"
    for s,t in o.items(): msg+=f"{s} #{t.get('attempt',1)} Entry ${t['entry']:.4f}\n"
    await u.message.reply_text(msg, parse_mode="HTML")

async def pnlnse_command(u,c):
    async with _lock: b=dict(BALANCE_NSE); o={k:v for k,v in PAPER_TRADES_NSE.items() if v.get('status')=='OPEN'}
    msg=f"🇮🇳 <b>NSE PNL (BOT3)</b>\nBal: ₹{b['total_balance']:.2f}\nStart: ₹{b['starting_balance']:.2f}\nPnL: {b['lifetime_pnl_percent']:.2f}% (₹{b['lifetime_pnl']:.2f})\nOpen: {len(o)} | Signals Today: {len(NSE_SIGNALS_TODAY)}\n"
    await u.message.reply_text(msg, parse_mode="HTML")

async def resetpnlcrypto_command(u,c):
    if not c.args or c.args[0].lower()!="confirm": return await u.message.reply_text("⚠️ <code>/resetpnlcrypto confirm</code>", parse_mode="HTML")
    async with _lock:
        BALANCE_CRYPTO['total_balance']=BALANCE_CRYPTO['starting_balance']; BALANCE_CRYPTO['lifetime_pnl_usdt']=0; BALANCE_CRYPTO['lifetime_pnl_percent']=0
        PAPER_TRADES.clear(); WATCHLIST.clear(); cooldown_coins.clear()
    cl=c.bot_data.get("http_client")
    if cl: await save_balance_crypto(cl); await save_paper_trades_crypto(cl); await save_watchlist(cl)
    await u.message.reply_text("✅ Crypto PNL Reset $10000", parse_mode="HTML")

async def resetpnlnse_command(u,c):
    if not c.args or c.args[0].lower()!="confirm": return await u.message.reply_text("⚠️ <code>/resetpnlnse confirm</code>", parse_mode="HTML")
    async with _lock:
        BALANCE_NSE['total_balance']=BALANCE_NSE['starting_balance']; BALANCE_NSE['lifetime_pnl']=0; BALANCE_NSE['lifetime_pnl_percent']=0
        PAPER_TRADES_NSE.clear(); ORB_LEVELS.clear(); NSE_SIGNALS_TODAY.clear()
    cl=c.bot_data.get("http_client")
    if cl: await save_balance_nse(cl); await save_paper_trades_nse(cl)
    await u.message.reply_text("✅ NSE PNL Reset ₹100000", parse_mode="HTML")

#... [BAKI AAPKE PURANE COMMANDS - add, remove, watchlist, open, close, exitall same rahenge, bas save function badlenge]...

# NOTE: check_paper_trades me ab BALANCE_CRYPTO use karo
# Example:
# BALANCE_CRYPTO['total_balance'] += nusdt

# BOT3 me jab NSE trade log karna ho to:
# PAPER_TRADES_NSE[symbol] = {...}
# BALANCE_NSE['total_balance'] += pnl