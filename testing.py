# COINDEX V9.0 SIMPLE - paper trading bot (backtest wali validated strategy)
# Strategy (simple_backtest_v11.py ke same):
#   24h pump >= PUMP_PERCENT_24H & turnover >= 10M  -> watchlist
#   ST cross below EMA300 (close dono ke neeche) -> trigger = cross bar low - 1.5 tick
#   agli bars me price trigger tod de -> SHORT, TP/SL fixed, 4h cooldown, retry nahi
import threading, asyncio, httpx, time, os, json, pandas as pd, numpy as np, logging, functools
from decimal import Decimal, ROUND_DOWN
from flask import Flask, jsonify, request
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

app = Flask(__name__)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

VERSION = "V9.0.4 SIMPLE"


def _env_f(name, default):
    try: return float(os.environ.get(name, default))
    except Exception: return default


# ==================== STRATEGY CONFIG ====================
# Render ke Environment me PUMP_PERCENT_24H / TP_PERCENT / SL_PERCENT / POSITION_SIZE_PERCENT se badal sakte hain
PUMP_PERCENT_24H = _env_f("PUMP_PERCENT_24H", 70)
TARGET_TP_PERCENT = _env_f("TP_PERCENT", 0.20)
SL_PERCENT = _env_f("SL_PERCENT", 0.10)
POSITION_SIZE_PERCENT = _env_f("POSITION_SIZE_PERCENT", 0.05)
MIN_TURNOVER_24H = 10_000_000
LEVERAGE = _env_f("LEVERAGE", 3)     # paper me sirf safety check ke liye; live ke liye yahi limit rakhein
MAX_OPEN_TRADES = int(_env_f("MAX_OPEN_TRADES", 4))      # env se badlein (8 test ke baad hi)
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
TRIGGER_TICK_OFFSET = 1.5
WARMUP_BARS = 300
WATCH_EXPIRY_SEC = _env_f("WATCH_EXPIRY_HOURS", 24) * 3600   # pump khatam hone ke itne baad hatao; 0 = kabhi nahi (untested)
COOLDOWN_SEC = 4 * 3600

# Safety: isolated margin me liquidation lagbhag (1/leverage) door hota hai (maintenance margin + fees ke baad kam).
# SL us se kaafi pehle lagna chahiye, warna SL se pehle liquidation ho sakta hai.
if LEVERAGE > 0 and SL_PERCENT > 0.6 / LEVERAGE:
    raise SystemExit(f"CONFIG ERROR: leverage {LEVERAGE:.0f}x par liquidation ~{100 / LEVERAGE:.0f}% door hai, "
                     f"SL {SL_PERCENT * 100:.0f}% bahut paas hai. LEVERAGE kam karein ya SL ghatayein.")

# fees / slippage (backtest ke same)
TAKER_FEE = 0.0005; GST_RATE = 0.18
EFFECTIVE_FEE_RATE = TAKER_FEE * (1 + GST_RATE)
# Slippage floors (fill me kam se kam itni). Default = backtest ka MEDIUM scenario (entry 0.2%, SL 0.5%).
# Base scenario (0.1% / 0.2%) ya harsh (0.3% / 1.0%) ke liye env se badlein: ENTRY_SLIP, EXIT_SLIP
SLIPPAGE_PCT = _env_f("ENTRY_SLIP", 0.002)
SL_EXTRA_SLIPPAGE = _env_f("EXIT_SLIP", 0.005)
BOOK_DEPTH_PENALTY = 0.01        # orderbook me depth kam ho to bacha hissa itna kharab price par

BOT1_SCAN_INTERVAL = int(_env_f("BOT1_SCAN_INTERVAL", 60))    # pump scanner (ek hi tickers call, sasta)
BOT2_SCAN_INTERVAL = int(_env_f("BOT2_SCAN_INTERVAL", 10))    # watchlist check
KLINE_LIMIT = 1000
KLINE_CACHE_TTL_ACTIVE = max(1, BOT2_SCAN_INTERVAL - 2)       # har cycle me fresh candles
LIVE_PRICE_CACHE_TTL = max(1, BOT2_SCAN_INTERVAL // 2)
SCAN_CONCURRENCY = 5                                          # ek saath kitne symbols (Bybit rate limit ke andar)
BOT12_STARTING_BALANCE = 10000.0

WEBHOOK_SECRET = os.environ.get("TELEGRAM_SECRET", "change_this_secret_123")
GIST_ID = os.environ.get("GIST_ID"); GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
GIST_URL = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else None
BOT_TOKEN = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_BOT_TOKEN = BOT_TOKEN; TELEGRAM_CHAT_ID = CHAT_ID
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")

WATCHLIST = {}; PAPER_TRADES = {}; TRADE_LOG = []
TICK_CACHE = {}; KLINE_CACHE = {}; LIVE_PRICE_CACHE = {}; cooldown_coins = {}
_lock = asyncio.Lock(); _gist_lock = asyncio.Lock()
BOT12_BALANCE_DATA = {"total_balance": BOT12_STARTING_BALANCE, "starting_balance": BOT12_STARTING_BALANCE,
                      "lifetime_pnl_usdt": 0.0, "lifetime_pnl_percent": 0.0}
application = None; main_event_loop = None; webhook_queue = asyncio.Queue()
BOT1_LAST_SCAN = 0; BOT2_LAST_SCAN = 0


# ==================== HELPERS ====================
def calc_pnl(entry, exit_price, amount_usdt):
    """Short PnL. FIX: exit notional = amount * exit/entry (pehle amount + gross tha, galat)."""
    ratio = exit_price / entry
    gross = amount_usdt * (1 - ratio)
    exit_value = amount_usdt * ratio
    fee = (amount_usdt + exit_value) * EFFECTIVE_FEE_RATE
    return gross - fee, gross, fee


def price_to_tick(price, tick):
    try:
        if not price or not tick or tick <= 0 or price <= 0: return float(price) if price else 0.0
        d_price = Decimal(str(price)); d_tick = Decimal(str(tick))
        if d_tick == 0: return float(price)
        return float((d_price / d_tick).to_integral_value(rounding=ROUND_DOWN) * d_tick)
    except Exception:
        return round(float(price), 8) if price else 0.0


def new_wl_entry(change, price, now):
    return {'time': now, 'last_pump': now, 'state': 'watching', 'break_level': 0.0, 'trig_ts': 0,
            'last_bar_ts': 0, 'max_change': change, 'pump_pct': change, 'wl_add_price': price}


def authorized_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not TELEGRAM_CHAT_ID: return await func(update, context, *args, **kwargs)
        user_id = str(update.effective_user.id); chat_id = str(update.effective_chat.id); allowed_id = str(TELEGRAM_CHAT_ID)
        if user_id != allowed_id and chat_id != allowed_id: return
        return await func(update, context, *args, **kwargs)
    return wrapper


# ==================== GIST STORAGE ====================
# SAFETY: Gist padhne me error aaye to bot KHAALI state se start nahi hota (warna agla save purana data overwrite kar deta).
# Load tab tak dobara try hota hai jab tak Gist sahi na padh liya jaye, aur Telegram par alert jata hai.
class GistError(Exception): pass

GIST_RETRY_SLEEP = 30          # startup me load fail ho to itne second baad dobara
GIST_BACKOFF = 1.0
GIST_STATUS = {'last_ok': 0.0, 'fails': 0, 'last_alert': 0.0}


def gist_enabled():
    return bool(GIST_URL and GITHUB_TOKEN)


def _json_default(o):
    if hasattr(o, 'item'):                       # numpy types
        try: return o.item()
        except Exception: pass
    return str(o)


async def gist_fetch_all(client, tries=4):
    """Poora gist ek call me: {filename: parsed_json}. Fail hone par GistError (khaali dict kabhi nahi)."""
    if not gist_enabled(): return {}
    last = ""
    for i in range(tries):
        try:
            r = await client.get(GIST_URL, headers=GIST_HEADERS, timeout=15.0)
            if r.status_code == 200:
                out = {}
                for name, f in ((r.json() or {}).get('files', {}) or {}).items():
                    if f.get('truncated'): raise GistError(f"{name} truncated (file bahut badi)")
                    content = f.get('content')
                    out[name] = json.loads(content) if content else {}
                return out
            last = f"HTTP {r.status_code}"
        except GistError: raise
        except Exception as e: last = f"{type(e).__name__}: {e}"
        await asyncio.sleep(GIST_BACKOFF * (2 ** i))
    raise GistError(last)


async def gist_set_locked(client, filename, content):
    if not gist_enabled(): return False
    ok = False; err = ""
    async with _gist_lock:
        try:
            payload = {"files": {filename: {"content": json.dumps(content, indent=2, default=_json_default)}}}
        except Exception as e:
            payload = None; err = f"serialize: {e}"
        if payload:
            for i in range(3):
                try:
                    r = await client.patch(GIST_URL, headers=GIST_HEADERS, json=payload, timeout=15.0)
                    if r.status_code == 200: ok = True; break
                    err = f"HTTP {r.status_code}"
                except Exception as e: err = f"{type(e).__name__}: {e}"
                await asyncio.sleep(GIST_BACKOFF * (i + 1))
    if ok:
        GIST_STATUS['last_ok'] = time.time()
        return True
    GIST_STATUS['fails'] += 1
    print(f"GIST SAVE FAIL {filename}: {err}", flush=True)
    now = time.time()
    if now - GIST_STATUS['last_alert'] > 600:        # 10 minute me ek hi alert
        GIST_STATUS['last_alert'] = now
        asyncio.create_task(send_telegram(client, f"⚠️ Gist save fail ({filename}): {err}. Abhi restart kiya to state mit sakta hai."))
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

async def save_trade_log(c):
    async with _lock: snapshot = list(TRADE_LOG)
    await gist_set_locked(c, 'trade_log.json', {'trades': snapshot})


async def load_watchlist(files):
    """Purane format ko naye state machine me convert karta hai (watching / waiting_break / short)."""
    global WATCHLIST
    data = files.get('watchlist.json') or {}
    now = time.time()
    async with _lock:
        WATCHLIST = {}
        for s_, d in ((data or {}).get('coins', {}) or {}).items():
            if not isinstance(d, dict): continue
            cs = s_.replace('.P', '')
            is_open = PAPER_TRADES.get(cs, {}).get('status') == 'OPEN'
            w = new_wl_entry(d.get('max_change', d.get('pump_pct', PUMP_PERCENT_24H)), d.get('wl_add_price', 0), d.get('time', now))
            w['last_pump'] = d.get('last_pump', d.get('time', now))
            old_state = d.get('state')
            w['state'] = 'short' if is_open else ('waiting_break' if old_state == 'waiting_break' else 'watching')
            if w['state'] == 'waiting_break':
                w['break_level'] = d.get('break_level', 0.0); w['trig_ts'] = d.get('trig_ts', 0)
            w['last_bar_ts'] = d.get('last_bar_ts', 0)
            WATCHLIST[cs] = w


async def load_paper_trades(files):
    global PAPER_TRADES
    data = files.get('paper_trades_crypto.json') or files.get('paper_trades.json') or {}
    async with _lock: PAPER_TRADES = data if isinstance(data, dict) else {}

async def load_bot12_balance(files):
    global BOT12_BALANCE_DATA
    d = files.get('bot12_pnl.json') or files.get('total_pnl.json')
    if d and 'total_balance' in d:
        async with _lock: BOT12_BALANCE_DATA = d

async def load_trade_log(files):
    global TRADE_LOG
    d = files.get('trade_log.json') or {}
    async with _lock: TRADE_LOG = list(d.get('trades', [])) if isinstance(d, dict) else []


async def load_all_state(client):
    """Gist sahi padhe jaane tak start nahi hota."""
    tries = 0
    while True:
        try:
            files = await gist_fetch_all(client)
            break
        except GistError as e:
            tries += 1
            print(f"GIST LOAD FAIL ({tries}): {e}", flush=True)
            if tries == 1 or tries % 10 == 0:
                await send_telegram(client, f"⚠️ Gist load fail ({e}). State padhe bina bot start nahi hoga (purana data bachane ke liye). Har {GIST_RETRY_SLEEP}s me dobara try.")
            await asyncio.sleep(GIST_RETRY_SLEEP)
    await load_paper_trades(files); await load_watchlist(files); await load_bot12_balance(files); await load_trade_log(files)
    GIST_STATUS['last_ok'] = time.time()
    return files


def startup_summary():
    open_syms = [k for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN']
    msg = (f"{VERSION} started | Gist {'ON' if gist_enabled() else 'OFF ⚠️'} | WL {len(WATCHLIST)} | "
           f"open {len(open_syms)} {open_syms} | closed {len(TRADE_LOG)} | bal ${BOT12_BALANCE_DATA['total_balance']:.2f}\n"
           f"pump>={PUMP_PERCENT_24H:.0f}% TP {TARGET_TP_PERCENT*100:.0f}% SL {SL_PERCENT*100:.0f}% size {POSITION_SIZE_PERCENT*100:.0f}% "
           f"lev {LEVERAGE:.0f}x slip {SLIPPAGE_PCT*100:.1f}/{SL_EXTRA_SLIPPAGE*100:.1f}%")
    if not gist_enabled():
        msg += "\n⚠️ GIST_ID / GITHUB_TOKEN set nahi hai. Restart par poora state (watchlist, open trades, balance) mit jayega."
    return msg


async def send_telegram(client, msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try: await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10.0)
    except Exception: pass


# ==================== TRADE CLOSE (lock ke andar call karein) ====================
def _close_trade_locked(symbol, exit_price, reason, extra=None, cooldown=True):
    tr = PAPER_TRADES[symbol]
    amt = tr.get('trade_amount_usdt') or tr.get('balance_at_entry', BOT12_STARTING_BALANCE) * POSITION_SIZE_PERCENT
    nusdt, gross, fee = calc_pnl(tr['entry'], exit_price, amt)
    b = BOT12_BALANCE_DATA
    b['total_balance'] += nusdt
    b['lifetime_pnl_usdt'] = b['total_balance'] - b['starting_balance']
    b['lifetime_pnl_percent'] = (b['lifetime_pnl_usdt'] / b['starting_balance'] * 100) if b['starting_balance'] else 0
    tr['status'] = f'CLOSED_{reason}'; tr['pnl_usdt'] = round(nusdt, 2); tr['exit'] = exit_price; tr['close_time'] = time.time()
    rec = {'symbol': symbol, 'entry': tr['entry'], 'exit': exit_price, 'tp': tr['tp'], 'sl': tr['sl'], 'reason': reason,
           'pnl': round(nusdt, 4), 'fee': round(fee, 4), 'amount': amt, 'open_ts': tr.get('time'), 'close_ts': time.time(),
           'max_change': tr.get('max_change'), 'entry_slip_pct': tr.get('entry_slip_pct'), 'book_slip_pct': tr.get('book_slip_pct')}
    if extra: rec.update(extra)
    TRADE_LOG.append(rec); del TRADE_LOG[:-2000]
    WATCHLIST.pop(symbol, None)
    if cooldown: cooldown_coins[symbol] = time.time() + COOLDOWN_SEC
    KLINE_CACHE.pop(symbol, None); LIVE_PRICE_CACHE.pop(symbol, None)
    return nusdt


async def _save_after_close(client):
    await save_bot12_balance(client); await save_paper_trades(client); await save_trade_log(client); await save_watchlist(client)


# ==================== TELEGRAM COMMANDS ====================
@authorized_only
async def start_command(u, c): await u.message.reply_text(f"{VERSION} ✅")

@authorized_only
async def add_command(u, c):
    if c.args:
        s = c.args[0].upper().replace('.P', '')
        async with _lock: WATCHLIST[s] = new_wl_entry(PUMP_PERCENT_24H, 0, time.time())
        cl = c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"{s} added")

@authorized_only
async def remove_command(u, c):
    if c.args:
        s = c.args[0].upper().replace('.P', '')
        async with _lock:
            if PAPER_TRADES.get(s, {}).get('status') == 'OPEN':
                is_open = True
            else:
                is_open = False
                WATCHLIST.pop(s, None); cooldown_coins.pop(s, None); KLINE_CACHE.pop(s, None); LIVE_PRICE_CACHE.pop(s, None)
        if is_open: return await u.message.reply_text(f"{s} has OPEN trade, use /close first")
        cl = c.bot_data.get("http_client")
        if cl: await save_watchlist(cl)
        await u.message.reply_text(f"{s} removed")

@authorized_only
async def removeall_command(u, c):
    if not c.args or c.args[0].lower() != "confirm":
        return await u.message.reply_text("Use /removeall confirm - open trades kept")
    async with _lock:
        open_syms = [k for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN']
        to_remove = [s for s in WATCHLIST if s not in open_syms]
        for s in to_remove:
            WATCHLIST.pop(s, None); cooldown_coins.pop(s, None); KLINE_CACHE.pop(s, None); LIVE_PRICE_CACHE.pop(s, None)
    cl = c.bot_data.get("http_client")
    if cl: await save_watchlist(cl)
    await u.message.reply_text(f"Cleared {len(to_remove)} coins, kept {len(open_syms)} open trades ✅")

@authorized_only
async def watchlist_command(u, c):
    msg = ""; now = time.time()
    async with _lock:
        n = len(WATCHLIST)
        for s, d in WATCHLIST.items():
            st = d.get('state', 'watching'); extra = ""
            if st == 'waiting_break': extra = f" break-level ${d.get('break_level', 0):.8f}"
            age = int((now - d.get('last_pump', now)) / 60)
            msg += f"{s} {st}{extra} max={d.get('max_change', 0):.0f}% pump-age={age}m\n"
    await u.message.reply_text(f"WL({n}) {VERSION}:\n{msg or 'Empty'}")

@authorized_only
async def health_command(u, c):
    async with _lock:
        b12 = dict(BOT12_BALANCE_DATA); wl = len(WATCHLIST)
        open_c = len([k for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN']); closed = len(TRADE_LOG)
    now = time.time()
    b1 = int(now - BOT1_LAST_SCAN) if BOT1_LAST_SCAN else 999; b2 = int(now - BOT2_LAST_SCAN) if BOT2_LAST_SCAN else 999
    await u.message.reply_text(f"HEALTH {VERSION}\nBOT1:{b1}s/{BOT1_SCAN_INTERVAL}s | BOT2:{b2}s/{BOT2_SCAN_INTERVAL}s\n"
                               f"WL:{wl} Open:{open_c}/{MAX_OPEN_TRADES} Closed:{closed} Bal:${b12['total_balance']:.2f}\n"
                               f"Cfg: pump>={PUMP_PERCENT_24H:.0f}% TP {TARGET_TP_PERCENT*100:.0f}% SL {SL_PERCENT*100:.0f}% size {POSITION_SIZE_PERCENT*100:.0f}% lev {LEVERAGE:.0f}x slots {MAX_OPEN_TRADES} expiry {WATCH_EXPIRY_SEC / 3600:.0f}h\n"
                               f"Slip floor: entry {SLIPPAGE_PCT*100:.1f}% SL {SL_EXTRA_SLIPPAGE*100:.1f}%\n"
                               f"Gist: {('ON last-ok ' + str(int(now - GIST_STATUS['last_ok'])) + 's ago, fails ' + str(GIST_STATUS['fails'])) if gist_enabled() else 'OFF - restart par state mit jayega'}")

@authorized_only
async def open_command(u, c):
    async with _lock: o = {k: v for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN'}
    if not o: return await u.message.reply_text("No Open")
    msg = f"OPEN ({len(o)})\n"
    for s, t in o.items(): msg += f"{s} Entry ${t['entry']:.8f} TP ${t['tp']:.8f} SL ${t['sl']:.8f}\n"
    await u.message.reply_text(msg)

@authorized_only
async def pnl_command(u, c):
    async with _lock: b = dict(BOT12_BALANCE_DATA)
    await u.message.reply_text(f"PNL {VERSION} ${b['total_balance']:.2f} ({b['lifetime_pnl_percent']:.2f}%)")

@authorized_only
async def stats_command(u, c):
    async with _lock: log = list(TRADE_LOG)
    if not log: return await u.message.reply_text("Abhi koi closed trade nahi")
    n = len(log); wins = [t['pnl'] for t in log if t['pnl'] > 0]; losses = [abs(t['pnl']) for t in log if t['pnl'] <= 0]
    wr = len(wins) / n * 100
    aw = sum(wins) / len(wins) if wins else 0; al = sum(losses) / len(losses) if losses else 0
    be = al / (aw + al) * 100 if (aw + al) > 0 else 0
    avg = lambda xs: (sum(xs) / len(xs)) if xs else 0.0
    es = [t['entry_slip_pct'] for t in log if t.get('entry_slip_pct') is not None]
    xs = [t['exit_slip_pct'] for t in log if t.get('reason') == 'SL' and t.get('exit_slip_pct') is not None]
    eb = [t['book_slip_pct'] for t in log if t.get('book_slip_pct') is not None]
    xb = [t['exit_book_slip_pct'] for t in log if t.get('exit_book_slip_pct') is not None]
    net = sum(t['pnl'] for t in log)
    msg = (f"STATS ({n} trades)\nWin {wr:.1f}% | Break-even {be:.1f}% | Edge {wr - be:+.1f}\n"
           f"Net ${net:+.2f} | avg win ${aw:.2f} | avg loss ${al:.2f}\n"
           f"Slippage fill me lagi: entry {avg(es):.2f}% | SL {avg(xs):.2f}% (floor {SLIPPAGE_PCT * 100:.1f}% / {SL_EXTRA_SLIPPAGE * 100:.1f}%)\n"
           f"Orderbook se NAPA hua (floor ke bina): entry {avg(eb):.2f}% ({len(eb)}) | SL {avg(xb):.2f}% ({len(xb)})\n"
           f"Backtest ref TP20/SL10/P70: medium slip win 40.8% BE 35.4% edge 5.4 | harsh win 39.8% BE 36.7% edge 3.1")
    await u.message.reply_text(msg)

@authorized_only
async def close_command(u, c):
    if not c.args: return await u.message.reply_text("Use /close SYM")
    s = c.args[0].upper().replace('.P', ''); cl = c.bot_data.get("http_client")
    live, _ = await get_live_price_cached(cl, s)
    if not live: return await u.message.reply_text("Price fetch failed")
    async with _lock:
        if PAPER_TRADES.get(s, {}).get('status') != 'OPEN': return await u.message.reply_text("No open")
        nusdt = _close_trade_locked(s, live, 'MANUAL')
    if cl: await _save_after_close(cl)
    await u.message.reply_text(f"Closed {s} ${nusdt:.2f}")

@authorized_only
async def exitall_command(u, c):
    cl = c.bot_data.get("http_client")
    async with _lock: open_syms = [k for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN']
    if not open_syms: return await u.message.reply_text("No Open")
    total = 0; cnt = 0
    for s in open_syms:
        live, _ = await get_live_price_cached(cl, s)
        if not live: continue
        async with _lock:
            if PAPER_TRADES.get(s, {}).get('status') != 'OPEN': continue
            total += _close_trade_locked(s, live, 'EXITALL'); cnt += 1
    if cl: await _save_after_close(cl)
    await u.message.reply_text(f"EXIT ALL {cnt} ${total:.2f}")

@authorized_only
async def exit_command(u, c):
    if not c.args: return await u.message.reply_text("Use /exit SYM/ALL")
    if c.args[0].upper() == "ALL": return await exitall_command(u, c)
    await close_command(u, c)

@authorized_only
async def resetpnl_command(u, c):
    if not c.args or c.args[-1].lower() != "confirm": return await u.message.reply_text("Use /resetpnl bot12 confirm")
    async with _lock:
        BOT12_BALANCE_DATA['total_balance'] = BOT12_BALANCE_DATA['starting_balance']
        BOT12_BALANCE_DATA['lifetime_pnl_usdt'] = 0.0; BOT12_BALANCE_DATA['lifetime_pnl_percent'] = 0.0
        TRADE_LOG.clear()
    cl = c.bot_data.get("http_client")
    if cl: await save_bot12_balance(cl); await save_trade_log(cl)
    await u.message.reply_text("PNL + trade log RESET")

@authorized_only
async def help_command(u, c):
    await u.message.reply_text(f"{VERSION}\n/watchlist /open /pnl /stats /health\n/add SYM /remove SYM /removeall confirm\n/close SYM /exit ALL /resetpnl bot12 confirm")


# ==================== BYBIT DATA ====================
async def get_klines_bybit_async(client, symbol, interval='5', limit=KLINE_LIMIT, include_current=False):
    url = "https://api.bybit.com/v5/market/kline"; by = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    params = {'category': 'linear', 'symbol': by, 'interval': interval, 'limit': limit}
    try:
        res = await client.get(url, params=params, timeout=10.0); data = res.json()
        if data.get('retCode') == 0 and data['result']['list']:
            df = pd.DataFrame(data['result']['list'], columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            df = df.astype({'timestamp': 'int64', 'open': float, 'high': float, 'low': float, 'close': float, 'volume': float, 'turnover': float})
            df = df.iloc[::-1].reset_index(drop=True)
            if not include_current: df = df.iloc[:-1].reset_index(drop=True)
            if len(df) >= 50: return df
    except Exception: pass
    return None


async def get_tick_size(client, symbol):
    if symbol in TICK_CACHE: return TICK_CACHE[symbol]
    url = "https://api.bybit.com/v5/market/instruments-info"; by = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    try:
        res = await client.get(url, params={'category': 'linear', 'symbol': by}, timeout=10.0); data = res.json()
        if data.get('retCode') == 0 and data['result']['list']:
            tick = float(data['result']['list'][0]['priceFilter']['tickSize'])
            if tick > 0: TICK_CACHE[symbol] = tick; return tick
    except Exception: return None
    return None


async def get_live_price(client, symbol):
    url = "https://api.bybit.com/v5/market/tickers"; by = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    try:
        res = await client.get(url, params={'category': 'linear', 'symbol': by}, timeout=5.0); data = res.json()
        if data.get('retCode') == 0 and data['result']['list']:
            r = data['result']['list'][0]
            lp = float(r['lastPrice']); ch = float(r.get('price24hPcnt', 0)) * 100
            return lp, ch, float(r.get('highPrice24h', lp))
    except Exception: pass
    return None, None, None


async def get_klines_cached(client, symbol):
    now = time.time()
    e = KLINE_CACHE.get(symbol)
    if e and now - e['time'] < KLINE_CACHE_TTL_ACTIVE: return e['df']
    df = await get_klines_bybit_async(client, symbol, interval='5', limit=KLINE_LIMIT, include_current=True)
    if df is not None: KLINE_CACHE[symbol] = {'df': df, 'time': now}
    return df


async def get_live_price_cached(client, symbol):
    now = time.time()
    e = LIVE_PRICE_CACHE.get(symbol)
    if e and now - e['time'] < LIVE_PRICE_CACHE_TTL: return e['price'], e.get('change', 0)
    price, change, high24 = await get_live_price(client, symbol)
    if price and price > 0: LIVE_PRICE_CACHE[symbol] = {'price': price, 'change': change, 'high24': high24, 'time': now}
    return price, change


async def book_fill(client, symbol, side, notional_usdt):
    """Orderbook walk: side 'sell' (short entry) bids khata hai, 'buy' (SL exit) asks.
    VWAP fill price return karta hai. Depth kam ho to bacha hissa 1% kharab price par maana jata hai."""
    by = symbol if symbol.endswith('USDT') else f"{symbol}USDT"
    try:
        res = await client.get("https://api.bybit.com/v5/market/orderbook",
                              params={'category': 'linear', 'symbol': by, 'limit': 50}, timeout=5.0)
        d = res.json()
        if d.get('retCode') != 0: return None
        levels = d['result']['b'] if side == 'sell' else d['result']['a']
        if not levels: return None
        remaining = float(notional_usdt); cost = 0.0; qty = 0.0; last_p = None
        for p, q in levels:
            p = float(p); q = float(q); last_p = p
            take = min(remaining, p * q)
            cost += take; qty += take / p; remaining -= take
            if remaining <= 1e-9: break
        if remaining > 1e-6 and last_p:
            bad = last_p * (1 - BOOK_DEPTH_PENALTY) if side == 'sell' else last_p * (1 + BOOK_DEPTH_PENALTY)
            cost += remaining; qty += remaining / bad
        return cost / qty if qty > 0 else None
    except Exception:
        return None


def calculate_supertrend(df, period=ATR_PERIOD, multiplier=ATR_MULTIPLIER):
    df = df.copy()
    high, low, close = df['high'].values, df['low'].values, df['close'].values
    hl2 = (high + low) / 2.0
    h_l = high - low
    h_pc = np.abs(high - np.roll(close, 1)); l_pc = np.abs(low - np.roll(close, 1))
    h_pc[0] = h_l[0]; l_pc[0] = h_l[0]
    tr = np.maximum(h_l, np.maximum(h_pc, l_pc))
    atr = np.zeros(len(tr)); atr[0] = np.mean(tr[:period])
    for i in range(1, len(tr)): atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    upper = hl2 + multiplier * atr; lower = hl2 - multiplier * atr
    n = len(df); fu = np.zeros(n); fl = np.zeros(n)
    bull = np.ones(n, dtype=bool); st_line = np.zeros(n)
    for i in range(n):
        if i == 0:
            fu[i] = upper[i]; fl[i] = lower[i]; st_line[i] = upper[i]; bull[i] = False; continue
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1]) else fl[i - 1]
        ps = bull[i - 1]
        if ps and close[i] < fl[i]: bull[i] = False
        elif not ps and close[i] > fu[i]: bull[i] = True
        else: bull[i] = ps
        st_line[i] = fl[i] if bull[i] else fu[i]
    df['st_line'] = st_line
    df['ema_val'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    df['atr'] = atr
    return df


# ==================== EXIT CHECK ====================
async def check_paper_trades(client, df, symbol, live_price):
    """True return karta hai agar trade close hua."""
    try:
        async with _lock:
            tr = PAPER_TRADES.get(symbol)
            if not tr or tr.get('status') != 'OPEN': return False
            trade = dict(tr)
        entry_ms = int(trade.get('time', 0) * 1000)
        after = df[df['timestamp'] > entry_ms]       # sirf entry ke BAAD shuru hui bars
        hi = max(trade.get('hi_seen', trade['entry']), live_price)
        lo = min(trade.get('lo_seen', trade['entry']), live_price)
        if len(after):
            hi = max(hi, float(after['high'].max())); lo = min(lo, float(after['low'].min()))
        async with _lock:
            if symbol in PAPER_TRADES: PAPER_TRADES[symbol]['hi_seen'] = hi; PAPER_TRADES[symbol]['lo_seen'] = lo

        amt = trade.get('trade_amount_usdt') or trade['balance_at_entry'] * POSITION_SIZE_PERCENT
        if hi >= trade['sl']:                         # SL pehle (conservative)
            book_px = await book_fill(client, symbol, 'buy', amt * (1 + SL_PERCENT))
            floor_fill = max(trade['sl'], live_price) * (1 + SL_EXTRA_SLIPPAGE)
            fill = max(floor_fill, book_px) if book_px else floor_fill
            exit_slip = (fill - trade['sl']) / trade['sl'] * 100
            ref = max(trade['sl'], live_price)
            exit_book_slip = ((book_px - ref) / ref * 100) if book_px else None     # sirf orderbook se napa hua (floor ke bina)
            async with _lock:
                if PAPER_TRADES.get(symbol, {}).get('status') != 'OPEN': return False
                nusdt = _close_trade_locked(symbol, fill, 'SL', extra={'exit_slip_pct': exit_slip, 'exit_book_px': book_px,
                                                                      'exit_book_slip_pct': exit_book_slip})
            await _save_after_close(client)
            if book_px:
                book_txt = f"book slip {exit_book_slip:.2f}% (used {'book' if book_px > floor_fill else 'floor'})"
            else:
                book_txt = "book N/A (orderbook fail, floor use hua)"
            asyncio.create_task(send_telegram(client,
                f"SL {symbol} ${nusdt:.2f} fill ${fill:.8f} slip {exit_slip:.2f}%\n{book_txt} | cooldown 4h"))
            return True
        if lo <= trade['tp']:
            async with _lock:
                if PAPER_TRADES.get(symbol, {}).get('status') != 'OPEN': return False
                nusdt = _close_trade_locked(symbol, trade['tp'], 'TP', extra={'exit_slip_pct': 0.0})
            await _save_after_close(client)
            asyncio.create_task(send_telegram(client, f"TP {symbol} ${nusdt:.2f} cooldown 4h"))
            return True
    except Exception as e: print(f"check trades {symbol}: {e}", flush=True)
    return False


# ==================== BOT 1: PUMP SCANNER ====================
async def bot1_scan(client):
    global BOT1_LAST_SCAN
    print(f"Bot1 {VERSION} Operational", flush=True)
    while True:
        try:
            BOT1_LAST_SCAN = time.time()
            res = await client.get("https://api.bybit.com/v5/market/tickers?category=linear", timeout=20.0)
            data = res.json()
            if data.get('retCode') == 0 and data.get('result'):
                now = time.time(); pumped_now = set(); msgs = []; changed = False
                for t in data['result']['list']:
                    m = t.get('symbol', '')
                    if not m.endswith('USDT'): continue
                    s = m.replace('.P', '')
                    try:
                        ch = float(t.get('price24hPcnt', 0)) * 100; turnover = float(t.get('turnover24h', 0)); last_price = float(t.get('lastPrice', 0))
                    except Exception: continue
                    if turnover < MIN_TURNOVER_24H or ch < PUMP_PERCENT_24H: continue
                    pumped_now.add(s)
                    async with _lock:
                        if s in WATCHLIST:
                            WATCHLIST[s]['last_pump'] = now
                            if ch > WATCHLIST[s].get('max_change', 0): WATCHLIST[s]['max_change'] = ch
                            continue
                        if s in cooldown_coins:
                            if now < cooldown_coins[s]: continue
                            del cooldown_coins[s]
                        WATCHLIST[s] = new_wl_entry(ch, last_price, now); changed = True
                        msgs.append(f"👀 PUMP {s} +{ch:.1f}% added")
                # expiry: pump condition 24h se nahi mili
                async with _lock:
                    for s, w in list(WATCHLIST.items()):
                        if s in pumped_now or PAPER_TRADES.get(s, {}).get('status') == 'OPEN': continue
                        if WATCH_EXPIRY_SEC > 0 and now - w.get('last_pump', w.get('time', now)) > WATCH_EXPIRY_SEC:
                            del WATCHLIST[s]; KLINE_CACHE.pop(s, None); changed = True
                for m in msgs: asyncio.create_task(send_telegram(client, m))
                if changed: await save_watchlist(client)
        except Exception as e: print(f"Bot1 {e}", flush=True)
        await asyncio.sleep(BOT1_SCAN_INTERVAL)


# ==================== BOT 2: SIGNAL + ENTRY ====================
async def process_symbol(client, symbol):
    """True return karta hai agar watchlist badli (taaki save ho)."""
    try:
        df_raw = await get_klines_cached(client, symbol)
        if df_raw is None or len(df_raw) < WARMUP_BARS + 5: return False
        df = await asyncio.to_thread(calculate_supertrend, df_raw)   # causal: closed bars ki values wahi rehti hain
        live_price, _ = await get_live_price_cached(client, symbol)
        if not live_price or live_price <= 0: live_price = float(df['close'].iloc[-1])

        if await check_paper_trades(client, df, symbol, live_price): return True

        tick = await get_tick_size(client, symbol)
        if not tick or tick <= 0: return False

        closed = df.iloc[:-1]
        bar_ts = int(closed['timestamp'].iloc[-1])
        close = float(closed['close'].iloc[-1]); low_c = float(closed['low'].iloc[-1])
        st = float(closed['st_line'].iloc[-1]); ema = float(closed['ema_val'].iloc[-1])
        p_st = float(closed['st_line'].iloc[-2]); p_ema = float(closed['ema_val'].iloc[-2])
        live_low = float(df['low'].iloc[-1])

        changed = False; signal = None; msgs = []
        async with _lock:
            w = WATCHLIST.get(symbol)
            if not w or PAPER_TRADES.get(symbol, {}).get('status') == 'OPEN': return False
            if w.get('state') == 'short': w['state'] = 'watching'      # stale state (trade nahi hai)

            # --- bar close event (har nayi closed bar par ek baar) ---
            if bar_ts > w.get('last_bar_ts', 0):
                first_seen = w.get('last_bar_ts', 0) == 0
                w['last_bar_ts'] = bar_ts
                if not first_seen:
                    if w['state'] == 'waiting_break':
                        if bar_ts > w['trig_ts'] and low_c <= w['break_level']:
                            signal = {'level': w['break_level']}           # last 20s me break miss hua to yahan pakdo
                        elif close > st or close > ema:
                            w['state'] = 'watching'; w['break_level'] = 0.0; changed = True
                            msgs.append(f"Cancel trigger {symbol} close above ST/EMA")
                    elif w['state'] == 'watching':
                        if close < ema and close < st and st < ema and p_st >= p_ema:
                            w['state'] = 'waiting_break'
                            w['break_level'] = low_c - tick * TRIGGER_TICK_OFFSET
                            w['trig_ts'] = bar_ts; changed = True
                            msgs.append(f"ST/EMA cross {symbol} armed. Break level ${w['break_level']:.8f}")

            # --- intrabar break (har poll par) ---
            if signal is None and w['state'] == 'waiting_break' and live_low <= w['break_level']:
                signal = {'level': w['break_level']}

            if signal is not None:
                active = sum(1 for t in PAPER_TRADES.values() if t.get('status') == 'OPEN')
                if active >= MAX_OPEN_TRADES: signal = None               # slot nahi, trigger armed rehta hai
                else: signal['tamt'] = BOT12_BALANCE_DATA['total_balance'] * POSITION_SIZE_PERCENT
            max_change = w.get('max_change', 0)

        for m in msgs: asyncio.create_task(send_telegram(client, m))
        if signal is None: return changed

        # --- entry: orderbook se asli fill estimate (network call, lock ke bahar) ---
        level = signal['level']
        book_px = await book_fill(client, symbol, 'sell', signal['tamt'])
        base = min(live_price, level)
        est = base * (1 - SLIPPAGE_PCT)
        fill = min(est, book_px) if book_px else est
        ep = price_to_tick(fill, tick); tp = price_to_tick(ep * (1 - TARGET_TP_PERCENT), tick); sl = price_to_tick(ep * (1 + SL_PERCENT), tick)
        if ep <= 0 or tp <= 0 or sl <= 0: return changed

        opened = False
        async with _lock:
            w = WATCHLIST.get(symbol)
            active = sum(1 for t in PAPER_TRADES.values() if t.get('status') == 'OPEN')
            if w and PAPER_TRADES.get(symbol, {}).get('status') != 'OPEN' and active < MAX_OPEN_TRADES:
                tamt = BOT12_BALANCE_DATA['total_balance'] * POSITION_SIZE_PERCENT
                PAPER_TRADES[symbol] = {
                    'entry': ep, 'tp': tp, 'sl': sl, 'status': 'OPEN', 'time': time.time(),
                    'balance_at_entry': BOT12_BALANCE_DATA['total_balance'], 'trade_amount_usdt': tamt, 'attempt': 1,
                    'break_level': level, 'live_at_signal': live_price, 'book_px_entry': book_px,
                    'entry_slip_pct': (level - ep) / level * 100,
                    'book_slip_pct': ((live_price - book_px) / live_price * 100) if book_px else None,
                    'max_change': max_change, 'hi_seen': ep, 'lo_seen': ep}
                w['state'] = 'short'; w['break_level'] = 0.0; opened = True
        if opened:
            await save_paper_trades(client)
            if book_px:
                book_txt = (f"book fill ${book_px:.8f} ({(live_price - book_px) / live_price * 100:.2f}% vs live, "
                            f"used {'book' if book_px < est else 'floor'})")
            else:
                book_txt = "book N/A (orderbook fail, floor use hua)"
            asyncio.create_task(send_telegram(client,
                f"SHORT {symbol} Entry ${ep:.8f} TP ${tp:.8f} SL ${sl:.8f}\n"
                f"slip vs break {(level - ep) / level * 100:.2f}% | {book_txt} | max={max_change:.0f}%"))
            return True
        return changed
    except Exception as e:
        print(f"proc {symbol} {e}", flush=True); return False


async def scan_cycle(client):
    """Watchlist ke saare symbols ek cycle me (SCAN_CONCURRENCY ek saath). Return: kitne symbols check hue."""
    async with _lock:
        syms = sorted(set(WATCHLIST.keys()) | {k for k, v in PAPER_TRADES.items() if v.get('status') == 'OPEN'})
    if not syms: return 0
    sem = asyncio.Semaphore(SCAN_CONCURRENCY)

    async def one(sym):
        async with sem:
            try:
                if await process_symbol(client, sym): await save_watchlist(client)
            except Exception as e: print(f"bot2 loop {sym} {e}", flush=True)
    await asyncio.gather(*[one(x) for x in syms])
    return len(syms)


async def bot2_scan(client):
    global BOT2_LAST_SCAN
    print(f"Bot2 {VERSION} Operational (every {BOT2_SCAN_INTERVAL}s)", flush=True)
    while True:
        t0 = time.time()
        try:
            BOT2_LAST_SCAN = t0
            n = await scan_cycle(client)
            if n == 0:
                await asyncio.sleep(5); continue
        except Exception as e: print(f"Bot2 {e}", flush=True)
        await asyncio.sleep(max(1.0, BOT2_SCAN_INTERVAL - (time.time() - t0)))


# ==================== WEB / MAIN ====================
@app.route('/')
def home(): return jsonify({"status": VERSION, "watchlist": len(WATCHLIST), "closed_trades": len(TRADE_LOG)})

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        if WEBHOOK_SECRET != "change_this_secret_123":
            if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET: return jsonify({"ok": False}), 403
        data = request.get_json(force=True, silent=True)
        if data and main_event_loop: main_event_loop.call_soon_threadsafe(webhook_queue.put_nowait, data)
    except Exception: pass
    return jsonify({"ok": True}), 200

async def process_webhook_queue():
    while True:
        data = await webhook_queue.get()
        try:
            if application and application.bot:
                await application.process_update(Update.de_json(data, application.bot))
        except Exception: pass
        finally: webhook_queue.task_done()

async def main_async():
    global application, main_event_loop
    main_event_loop = asyncio.get_running_loop()
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    async with httpx.AsyncClient(limits=limits) as client:
        await load_all_state(client)
        print(f"Loaded {VERSION} WL={len(WATCHLIST)} Trades={len(TRADE_LOG)} Bal ${BOT12_BALANCE_DATA['total_balance']:.2f}", flush=True)
        t_req = HTTPXRequest(connection_pool_size=20, connect_timeout=30.0, read_timeout=30.0, write_timeout=30.0)
        app_t = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
        app_t.bot_data["http_client"] = client
        application = app_t
        for cmd, fn in [("start", start_command), ("add", add_command), ("remove", remove_command), ("removeall", removeall_command),
                        ("watchlist", watchlist_command), ("open", open_command), ("close", close_command), ("pnl", pnl_command),
                        ("stats", stats_command), ("exit", exit_command), ("exitall", exitall_command), ("resetpnl", resetpnl_command),
                        ("reset", resetpnl_command), ("help", help_command), ("health", health_command)]:
            app_t.add_handler(CommandHandler(cmd, fn))
        await app_t.initialize(); await app_t.start()
        asyncio.create_task(process_webhook_queue())
        if WEBHOOK_URL:
            wh_url = f"{WEBHOOK_URL.rstrip('/')}/webhook"
            try:
                await app_t.bot.delete_webhook(drop_pending_updates=True); await asyncio.sleep(1)
                await app_t.bot.set_webhook(url=wh_url, drop_pending_updates=True,
                                            secret_token=WEBHOOK_SECRET if WEBHOOK_SECRET != "change_this_secret_123" else None)
            except Exception: pass
        else:
            try: await app_t.bot.delete_webhook(drop_pending_updates=True)
            except Exception: pass
            await asyncio.sleep(5)
            await app_t.updater.start_polling(drop_pending_updates=True, poll_interval=2.0, bootstrap_retries=-1)
        port = int(os.environ.get("PORT", 10000))
        threading.Thread(target=lambda: app.run(host='0.0.0.0', port=port, use_reloader=False), daemon=True).start()
        asyncio.create_task(bot1_scan(client)); asyncio.create_task(bot2_scan(client))
        asyncio.create_task(send_telegram(client, startup_summary()))
        print(f"{VERSION} Operational", flush=True)
        while True: await asyncio.sleep(3600)

def main():
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main_async())

if __name__ == '__main__': main()
