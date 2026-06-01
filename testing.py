import requests
import time
import os
from flask import Flask, jsonify
import threading
import pandas as pd
import numpy as np
import json
import math

app = Flask(__name__)

PUMP_PERCENT_24H = 40       # Bot1 trigger - 24H Change
WATCHLIST_DAYS   = 2        # 2 din tak monitor
ATR_PERIOD       = 10
ATR_MULTIPLIER   = 3
EMA_PERIOD       = 300
WATCHLIST_FILE   = "watchlist.json"

COINDX_FUTURES = {
    '0GUSDT', '00000MOGUSDT', '00BONKUSDT', '00CATUSDT', '00FLOKIUSDT',
    '00LUNCUSDT', '00PEUSDT', '00RATSUSDT', '00SATSUSDT', '00SHIBUSDT',
    '1INCHUSDT', '1MBABYDOGEUSDT', '2ZUSDT', 'AUSDT', 'AAVEUSDT', 'ACEUSDT',
    'ACHUSDT', 'ACTUSDT', 'ACUUSDT', 'ACXUSDT', 'ADAUSDT', 'AEROUSDT', 'AEVOUSDT',
    'AGLDUSDT', 'AIGENSYNUSDT', 'AIXBTUSDT', 'AKTUSDT', 'ALCHUSDT', 'ALGOUSDT',
    'ALICEUSDT', 'ALLOUSDT', 'ALPINEUSDT', 'ALTUSDT', 'ANIMEUSDT', 'APEUSDT',
    'API3USDT', 'APTUSDT', 'ARUSDT', 'ARBUSDT', 'ARCUSDT', 'ARKUSDT', 'ARKMUSDT',
    'ARPAUSDT', 'ASRUSDT', 'ASTERUSDT', 'ASTRUSDT', 'ATUSDT', 'ATHUSDT', 'ATOMUSDT',
    'AUCTIONUSDT', 'AVAUSDT', 'AVAAIUSDT', 'AVAXUSDT', 'AVNTUSDT', 'AWEUSDT',
    'AXLUSDT', 'AXSUSDT', 'BABYUSDT', 'BANUSDT', 'BANANAUSDT', 'BANANAS31USDT',
    'BANDUSDT', 'BARDUSDT', 'BASEDUSDT', 'BATUSDT', 'BBUSDT', 'BCHUSDT', 'BELUSDT',
    'BERAUSDT', 'BICOUSDT', 'BIGTIMEUSDT', 'BILLUSDT', 'BIOUSDT', 'BIRBUSDT',
    'BLURUSDT', 'BMTUSDT', 'BNBUSDT', 'BNTUSDT', 'BOMEUSDT', 'BRETTUSDT', 'BREVUSDT',
    'BROCCOLI714USDT', 'BSBUSDT', 'BSVUSDT', 'BTCUSDT', 'BZUSDT', 'C98USDT',
    'CAKEUSDT', 'CARVUSDT', 'CATIUSDT', 'CCUSDT', 'CELOUSDT', 'CETUSUSDT', 'CFGUSDT',
    'CFXUSDT', 'CGPTUSDT', 'CHIPUSDT', 'CHILLGUYUSDT', 'CHRSUSDT', 'CHZUSDT', 'CKBUSDT',
    'CLUSDT', 'CLANKERUSDT', 'COMPUSDT', 'COOKIEUSDT', 'COSUSDT', 'COTIUSDT', 'COWUSDT',
    'CRVUSDT', 'CTSIUSDT', 'CYBERUSDT', 'DASHUSDT', 'DEEPUSDT', 'DEXEUSDT', 'DIAUSDT',
    'DOGEUSDT', 'DOGSUSDT', 'DOLOUSDT', 'DOTUSDT', 'DRIFTUSDT', 'DUSKUSDT', 'DYDXUSDT',
    'DYMUSDT', 'EDENUSDT', 'EDGEUSDT', 'EDUUSDT', 'EGLDUSDT', 'EIGENUSDT', 'ENAUSDT',
    'ENJUSDT', 'ENSUSDT', 'ENSOUSDT', 'EPICUSDT', 'ERAUSDT', 'ESPUSDT', 'ETCUSDT',
    'ETHUSDT', 'ETHFIUSDT', 'ETHWUSDT', 'EULUSDT', 'FUSDT', 'FARTCOINUSDT', 'FETUSDT',
    'FFUSDT', 'FHEUSDT', 'FIDAUSDT', 'FIGHTUSDT', 'FILUSDT', 'FLOCKUSDT', 'FLOWUSDT',
    'FLUIDUSDT', 'FLUXUSDT', 'FOGOUSDT', 'FORMUSDT', 'FRAXUSDT', 'GUSDT', 'GALAUSDT',
    'GASUSDT', 'GENIUSUSDT', 'GIGGLEUSDT', 'GLMUSDT', 'GMTUSDT', 'GMXUSDT', 'GOATUSDT',
    'GPSUSDT', 'GRASSUSDT', 'GRIFFAINUSDT', 'GRTUSDT', 'GTCUSDT', 'GUNUSDT', 'GWEIUSDT',
    'HUSDT', 'HAEDALUSDT', 'HBARUSDT', 'HEIUSDT', 'HEMIUSDT', 'HFTUSDT', 'HIGHUSDT',
    'HIVEUSDT', 'HMSTRUSDT', 'HOLOUSDT', 'HOMEUSDT', 'HOTUSDT', 'HUMAUSDT', 'HYPEUSDT',
    'HYPERUSDT', 'ICNTUSDT', 'ICPUSDT', 'ICXUSDT', 'IDUSDT', 'ILVUSDT', 'IMXUSDT',
    'INITUSDT', 'INJUSDT', 'INXUSDT', 'IOUSDT', 'IOSTUSDT', 'IOTAUSDT', 'IOTXUSDT',
    'IPUSDT', 'IRYSUSDT', 'JASMYUSDT', 'JOEUSDT', 'JSTUSDT', 'JTOUSDT', 'JUPUSDT',
    'KAIAUSDT', 'KAITOUSDT', 'KASUSDT', 'KATUSDT', 'KAVAUSDT', 'KERNELUSDT', 'KITEUSDT',
    'KMNOUSDT', 'KNCUSDT', 'KOMAUSDT', 'KSMUSDT', 'LAUSDT', 'LAYERUSDT', 'LDOUSDT',
    'LINEAUSDT', 'LINKUSDT', 'LISTAUSDT', 'LITUSDT', 'LPTUSDT', 'LQTYUSDT', 'LSKUSDT',
    'LUMIAUSDT', 'LUNA2USDT', 'LTCUSDT', 'MAGICUSDT', 'MAGMAUSDT', 'MANAUSDT', 'MANTAUSDT',
    'MANTRAUSDT', 'MASKUSDT', 'MAVUSDT', 'MAVIAUSDT', 'MBOXUSDT', 'MEUSDT', 'MEGAUSDT',
    'MELANIAUSDT', 'MEMEUSDT', 'MERLUSDT', 'METUSDT', 'METISUSDT', 'MEWUSDT', 'MINAUSDT',
    'MIRAUSDT', 'MITOUSDT', 'MMTUSDT', 'MOCAUSDT', 'MONUSDT', 'MOODENGUSDT', 'MORPHOUSDT',
    'MOVEUSDT', 'MOVRUSDT', 'MTLUSDT', 'MUBARAKUSDT', 'NATGASUSDT', 'NEARUSDT', 'NEOUSDT',
    'NEWTUSDT', 'NFPUSDT', 'NIGHTUSDT', 'NILUSDT', 'NMRUSDT', 'NOMUSDT', 'NOTUSDT',
    'NXPCUSDT', 'OGNUSDT', 'ONDOUSDT', 'ONEUSDT', 'ONGUSDT', 'ONTUSDT',
    'OPUSDT', 'OPENUSDT', 'OPNUSDT', 'ORCAUSDT', 'ORDIUSDT', 'PARTIUSDT', 'PAXGUSDT',
    'PENDLEUSDT', 'PENGUUSDT', 'PEOPLEUSDT', 'PHAUSDT', 'PIPPINUSDT', 'PIXELUSDT',
    'PLUMEUSDT', 'PNUTUSDT', 'POLYXUSDT', 'POPCATUSDT', 'PORTALUSDT',
    'POWERUSDT', 'POWRUSDT', 'PRLUSDT', 'PROMUSDT', 'PROVEUSDT', 'PUMPUSDT', 'PUNDIXUSDT',
    'PYTHUSDT', 'QNTUSDT', 'QTUMUSDT', 'RAREUSDT', 'RAVEUSDT', 'RECALLUSDT', 'REDUSDT',
    'RENDERUSDT', 'RESOLVUSDT', 'REZUSDT', 'RIFUSDT', 'RIVERUSDT', 'RLCUSDT', 'ROBOUSDT',
    'RONINUSDT', 'ROSEUSDT', 'RPLUSDT', 'RSRUSDT', 'RUNEUSDT', 'RVNUSDT', 'SUSDT',
    'SAFEUSDT', 'SAGAUSDT', 'SAHARAUSDT', 'SANDUSDT', 'SANTOSUSDT', 'SAPIENUSDT',
    'SCRUSDT', 'SCRTUSDT', 'SEIUSDT', 'SENTUSDT', 'SFPUSDT', 'SHELLUSDT', 'SIGNUSDT',
    'SKLUSDT', 'SKRUSDT', 'SKYUSDT', 'SNXUSDT', 'SOLVUSDT', 'SOMIUSDT',
    'SONICUSDT', 'SOPHUSDT', 'SPELLUSDT', 'SPKUSDT', 'SPXUSDT', 'SQDUSDT', 'SSVUSDT',
    'STABLEUSDT', 'STEEMUSDT', 'STGUSDT', 'STOUSDT', 'STORJUSDT', 'STRKUSDT', 'STXUSDT',
    'SUIUSDT', 'SUNUSDT', 'SUPERUSDT', 'SUSHIUSDT', 'SWARMSUSDT', 'SYNUSDT', 'SYRUPUSDT',
    'SXTUSDT', 'TUSDT', 'TAUSDT', 'TACUSDT', 'TAIKOUSDT', 'TAOUSDT', 'THEUSDT',
    'THETAUSDT', 'TIAUSDT', 'TLMUSDT', 'TNSRUSDT', 'TONUSDT', 'TOWNSUSDT', 'TRBUSDT',
    'TREEUSDT', 'TRIAUSDT', 'TRUMPUSDT', 'TRXUSDT', 'TURTLEUSDT', 'TUTUSDT', 'TWTUSDT',
    'UMAUSDT', 'UNIUSDT', 'USTCUSDT', 'USUALUSDT', 'USDCUSDT', 'VANAUSDT', 'VANRYUSDT',
    'VELODROMEUSDT', 'VETUSDT', 'VICUSDT', 'VIRTUALUSDT', 'VTHOUSDT', 'VVVUSDT', 'WUSDT',
    'WALUSDT', 'WAXPUSDT', 'WCTUSDT', 'WETUSDT', 'WIFUSDT', 'WLDUSDT', 'WLFIUSDT',
    'WOOUSDT', 'XAIUSDT', 'XANUSDT', 'XAGUSDT', 'XAUUSDT', 'XLMUSDT', 'XMRUSDT',
    'XPLUSDT', 'XRPUSDT', 'XTZUSDT', 'XVSUSDT', 'YBUSDT', 'YFIUSDT', 'YGGUSDT',
    'ZBTUSDT', 'ZECUSDT', 'ZENUSDT', 'ZEREBROUSDT', 'ZETAUSDT', 'ZILUSDT', 'ZKUSDT',
    'ZKCUSDT', 'ZKPUSDT', 'ZROUSDT', 'ZRXUSDT'
}

WATCHLIST = {}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("CHAT_ID")

# ─────────────────────────────────────────────────────────────
# WATCHLIST LOAD / SAVE
# ─────────────────────────────────────────────────────────────

def load_watchlist():
    global WATCHLIST
    current_config = {
        'pump': PUMP_PERCENT_24H,
        'ema':  EMA_PERIOD,
        'days': WATCHLIST_DAYS
    }
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                saved_config = data.get('_config', {})
                if saved_config != current_config:
                    print(f"Config changed. Old: {saved_config} New: {current_config}. Clearing watchlist.", flush=True)
                    WATCHLIST = {}
                    save_watchlist()
                else:
                    WATCHLIST = data.get('coins', {})
                    print(f"Loaded {len(WATCHLIST)} coins from watchlist.json", flush=True)
        else:
            WATCHLIST = {}
    except Exception as e:
        print(f"Load watchlist error: {e}", flush=True)
        WATCHLIST = {}


def save_watchlist():
    try:
        data = {
            '_config': {
                'pump': PUMP_PERCENT_24H,
                'ema':  EMA_PERIOD,
                'days': WATCHLIST_DAYS
            },
            'coins': WATCHLIST
        }
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Save watchlist error: {e}", flush=True)


# ─────────────────────────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────────────────────────

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)


# ─────────────────────────────────────────────────────────────
# SUPERTREND CALCULATE
# ─────────────────────────────────────────────────────────────

def calculate_supertrend(df, period=10, multiplier=3):
    df = df.copy()

    df['h-l']  = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low']  - df['close'].shift(1))
    df['tr']   = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)

    # Wilder's RMA — TradingView se match karta hai
    df['atr'] = df['tr'].ewm(alpha=1 / period, adjust=False).mean()

    hl2 = (df['high'] + df['low']) / 2
    df['upperband'] = hl2 + (multiplier * df['atr'])
    df['lowerband'] = hl2 - (multiplier * df['atr'])

    df['final_upperband'] = 0.0
    df['final_lowerband'] = 0.0
    df['supertrend']      = True
    df['st_line']         = 0.0

    for i in range(len(df)):
        if i == 0:
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
            df.loc[df.index[i], 'final_lowerband'] = df['lowerband'].iloc[i]
            df.loc[df.index[i], 'st_line']         = df['upperband'].iloc[i]
            continue

        if (df['upperband'].iloc[i] < df['final_upperband'].iloc[i - 1] or
                df['close'].iloc[i - 1] > df['final_upperband'].iloc[i - 1]):
            df.loc[df.index[i], 'final_upperband'] = df['upperband'].iloc[i]
        else:
            df.loc[df.index[i], 'final_upperband'] = df['final_upperband'].iloc[i - 1]

        if (df['lowerband'].iloc[i] > df['final_lowerband'].iloc[i - 1] or
                df['close'].iloc[i - 1] < df['final_lowerband'].iloc[i - 1]):
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

    df['ema_val'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df


# ─────────────────────────────────────────────────────────────
# GET KLINES
# FIX: Current open candle drop karo → iloc[:-1]
# Warna candle hamesha "close nahi hui" dikhegi
# ─────────────────────────────────────────────────────────────

def get_klines(symbol, interval='5', limit=351):
    # limit=351 isliye taaki drop karne ke baad 350 closed candles bachein
    url    = "https://api.bybit.com/v5/market/kline"
    params = {
        'category': 'linear',
        'symbol':   symbol,
        'interval': interval,
        'limit':    limit
    }
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res['retCode'] == 0:
            data = res['result']['list']
            df = pd.DataFrame(data,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'turnover'])
            df = df.astype({
                'timestamp': 'int64',
                'open':  float,
                'high':  float,
                'low':   float,
                'close': float
            })

            # Bybit latest-first data deta hai → reverse karo
            df = df.iloc[::-1].reset_index(drop=True)

            # ═══════════════════════════════════════════════════
            # MAIN FIX: Last row = current OPEN candle — drop karo!
            # Yahi wajah thi ki bot hamesha SKIP karta tha.
            # Close candle ka timestamp hamesha current time se
            # 290s se zyada pehle hoga → condition pass ho jayegi
            # ═══════════════════════════════════════════════════
            df = df.iloc[:-1].reset_index(drop=True)

            if len(df) < EMA_PERIOD + 50:
                print(f"Warning: {symbol} ke liye sirf {len(df)} candles, EMA{EMA_PERIOD} ke liye kam hain", flush=True)
                return None

            return df
    except Exception as e:
        print(f"Kline Error {symbol}: {e}", flush=True)
    return None


# ─────────────────────────────────────────────────────────────
# BOT 1 — 24H Pump Scanner
# FIX: Re-alert logic — agar coin watchlist mein hai aur
#      cross_count 0 hai (expire hua) toh dobara add hoga
#      Naya din → naya pump → naya alert milna chahiye
# ─────────────────────────────────────────────────────────────

def bot1_scan_bybit_futures():
    print("Bot1 Bybit Futures thread started", flush=True)

    # ── FIX: Track karo kaun se symbols aaj already alert ho chuke ──
    # Yeh set sirf current bot session mein rahega
    # Bot restart hone pe reset ho jayega — intentional
    alerted_symbols = set()

    while True:
        try:
            url     = "https://api.bybit.com/v5/market/tickers"
            params  = {'category': 'linear'}
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, params=params, headers=headers, timeout=20)
            data     = response.json()

            if data['retCode'] != 0:
                print(f"Bot1 API Error: {data}", flush=True)
                time.sleep(60)
                continue

            tickers    = data['result']['list']
            cdcx_count = 0
            pumped     = 0

            for ticker in tickers:
                symbol = ticker['symbol']
                if symbol not in COINDX_FUTURES:
                    continue

                cdcx_count += 1
                change_24h = float(ticker['price24hPcnt']) * 100

                # ── FIX: alerted_symbols check karo ──────────────────
                # Pehle: "symbol not in WATCHLIST" → agar watchlist mein
                #        tha toh alert nahi aata tha (VIC wali problem!)
                # Ab: alerted_symbols se track karo — har session mein
                #     ek baar alert aayega chahe watchlist mein ho ya na ho
                if change_24h >= PUMP_PERCENT_24H and symbol not in alerted_symbols:
                    alerted_symbols.add(symbol)

                    # Watchlist mein add/update karo
                    if symbol not in WATCHLIST:
                        WATCHLIST[symbol] = {
                            'time':        time.time(),
                            'cross_count': 0
                        }
                    # Agar already tha toh cross_count reset mat karo
                    # Sirf time update karo taaki expire na ho
                    else:
                        WATCHLIST[symbol]['time'] = time.time()

                    save_watchlist()
                    pumped += 1

                    cdcx_name = symbol.replace('USDT', '-USDT')
                    price     = ticker['lastPrice']
                    msg = (
                        f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n"
                        f"<b>Coin:</b> {cdcx_name}\n"
                        f"<b>24h Change:</b> +{change_24h:.2f}%\n"
                        f"<b>Price:</b> ${price}\n"
                        f"<b>Exchange:</b> CoinDCX Listed\n"
                        f"<b>Source:</b> Bybit Futures\n\n"
                        f"👀 Bot2 watchlist mein add kiya.\n"
                        f"⏳ {WATCHLIST_DAYS} din tak monitor karega."
                    )
                    send_telegram(msg)
                    print(f"Bot1 Alert: {cdcx_name} {change_24h:.2f}%", flush=True)

            print(f"Bot1: Checked {cdcx_count} CoinDCX pairs | "
                  f"Pumped (40%+): {pumped} | "
                  f"Watchlist: {len(WATCHLIST)} coins", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300)


# ─────────────────────────────────────────────────────────────
# BOT 2 — Supertrend SHORT Signal
# Condition: Price < Supertrend < EMA 300
# ─────────────────────────────────────────────────────────────

def bot2_supertrend_short():
    print("Bot2 Supertrend SHORT thread started", flush=True)

    while True:
        try:
            if not WATCHLIST:
                print("Bot2: Watchlist empty, 30s wait...", flush=True)
                time.sleep(30)
                continue

            print(f"\nBot2: ===== NEW CYCLE — {len(WATCHLIST)} coins =====", flush=True)
            to_remove = []

            for symbol, info in list(WATCHLIST.items()):
                cdcx_name = symbol.replace('USDT', '-USDT')

                # ── Watchlist expiry check ────────────────────────────
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400:
                    print(f"Bot2: [{cdcx_name}] Watchlist expire — remove", flush=True)
                    to_remove.append(symbol)
                    continue

                # ── Data fetch (already fixed in get_klines) ─────────
                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD + 2:
                    print(f"Bot2: [{cdcx_name}] SKIP — data nahi mila", flush=True)
                    continue

                # ── Indicators calculate karo ─────────────────────────
                try:
                    df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)
                except Exception as e:
                    print(f"Bot2: [{cdcx_name}] Supertrend error: {e}", flush=True)
                    continue

                st_line     = df['st_line'].iloc[-1]
                ema_val     = df['ema_val'].iloc[-1]
                close_price = df['close'].iloc[-1]
                last_ts     = df['timestamp'].iloc[-1]

                # ── NaN check ─────────────────────────────────────────
                if any(math.isnan(v) for v in [st_line, ema_val, close_price]):
                    print(f"Bot2: [{cdcx_name}] SKIP — NaN value", flush=True)
                    continue

                # ── Signal Condition: Price < ST < EMA ────────────────
                price_below_st  = close_price < st_line
                st_below_ema    = st_line < ema_val
                short_condition = price_below_st and st_below_ema

                print(
                    f"Bot2: [{cdcx_name}] "
                    f"Price={close_price:.6f} | "
                    f"ST={st_line:.6f} | "
                    f"EMA{EMA_PERIOD}={ema_val:.6f} | "
                    f"Price<ST={price_below_st} | "
                    f"ST<EMA={st_below_ema} | "
                    f"SIGNAL={short_condition}",
                    flush=True
                )

                if short_condition:
                    cross_count = info.get('cross_count', 0)

                    if cross_count >= 3:
                        print(f"Bot2: [{cdcx_name}] Signal mila but limit 3/3 reach — skip", flush=True)
                    else:
                        msg = (
                            f"🔻 <b>BOT 2: SHORT SIGNAL #{cross_count + 1}</b> 🔻\n\n"
                            f"<b>Coin:</b> {cdcx_name}\n"
                            f"<b>Condition:</b> Price &lt; ST &lt; EMA{EMA_PERIOD}\n"
                            f"<b>Signal:</b> #{cross_count + 1}/3\n"
                            f"<b>Timeframe:</b> 5min\n\n"
                            f"<b>Price:</b> ${close_price:.6f}\n"
                            f"<b>ST(10,3):</b> ${st_line:.6f}\n"
                            f"<b>EMA{EMA_PERIOD}:</b> ${ema_val:.6f}\n\n"
                            f"📊 Price &lt; Supertrend &lt; EMA{EMA_PERIOD}\n"
                            f"🎯 CoinDCX Futures pe SHORT entry zone.\n"
                            f"🛑 SL: ST ke upar → ${st_line:.6f}"
                        )
                        send_telegram(msg)
                        WATCHLIST[symbol]['cross_count'] = cross_count + 1
                        save_watchlist()
                        print(f"Bot2: [{cdcx_name}] ✅ SHORT ALERT #{cross_count + 1} BHEJA!", flush=True)

                time.sleep(1)

            for symbol in to_remove:
                WATCHLIST.pop(symbol, None)
                save_watchlist()

            print(f"Bot2: ===== CYCLE COMPLETE — 30s wait =====\n", flush=True)

        except Exception as e:
            import traceback
            print(f"Bot2 Error: {e}", flush=True)
            print(traceback.format_exc(), flush=True)

        time.sleep(30)


# ─────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────

@app.route('/')
def home():
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins. CoinDCX Filter: ON"

@app.route('/watchlist')
def show_watchlist():
    return jsonify(WATCHLIST)


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    print(f"CoinDCX Futures loaded: {len(COINDX_FUTURES)} pairs", flush=True)

    load_watchlist()

    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    threading.Thread(target=bot2_supertrend_short,   daemon=True).start()

    app.run(host='0.0.0.0', port=10000)