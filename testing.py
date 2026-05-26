import requests
import time
import os
from flask import Flask
import threading
import pandas as pd
import numpy as np
import json

app = Flask(__name__)

PUMP_PERCENT_24H = 40 # Bot1 trigger - 24H Change
WATCHLIST_DAYS = 2 # 2 din tak monitor
ATR_PERIOD = 10
ATR_MULTIPLIER = 3
EMA_PERIOD = 300
WATCHLIST_FILE = "watchlist.json" # FIX 1: File me save

# CoinDCX Futures List - Teri di hui list
COINDX_FUTURES = {
    '0GUSDT', '1000000MOGUSDT', '1000BONKUSDT', '1000CATUSDT', '1000FLOKIUSDT',
    '1000LUNCUSDT', '1000PEPEUSDT', '1000RATSUSDT', '1000SATSUSDT', '1000SHIBUSDT',
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
    'CFXUSDT', 'CGPTUSDT', 'CHIPUSDT', 'CHILLGUYUSDT', 'CHRUSDT', 'CHZUSDT', 'CKBUSDT',
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

WATCHLIST = {} # {'BSBUSDT': {'time': 123456, 'last_st': None}}
TELEGRAM_BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("CHAT_ID")

# ===== FIX 1: WATCHLIST SAVE/LOAD FUNCTIONS =====
def load_watchlist():
    global WATCHLIST
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, 'r') as f:
                WATCHLIST = json.load(f)
                print(f"Loaded {len(WATCHLIST)} coins from watchlist.json", flush=True)
        else:
            WATCHLIST = {}
    except Exception as e:
        print(f"Load watchlist error: {e}", flush=True)
        WATCHLIST = {}

def save_watchlist():
    try:
        with open(WATCHLIST_FILE, 'w') as f:
            json.dump(WATCHLIST, f)
    except Exception as e:
        print(f"Save watchlist error: {e}", flush=True)
# ================================================

def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}", flush=True)

def calculate_supertrend(df, period=10, multiplier=3):
    df['h-l'] = df['high'] - df['low']
    df['h-pc'] = abs(df['high'] - df['close'].shift(1))
    df['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['h-l', 'h-pc', 'l-pc']].max(axis=1)
    df['atr'] = df['tr'].rolling(period).mean()

    df['upperband'] = (df['high'] + df['low']) / 2 + multiplier * df['atr']
    df['lowerband'] = (df['high'] + df['low']) / 2 - multiplier * df['atr']

    df['final_upperband'] = df['upperband']
    df['final_lowerband'] = df['lowerband']

    for i in range(1, len(df)):
        if df['close'].iloc[i-1] <= df['final_upperband'].iloc[i-1]:
            df.loc[df.index[i], 'final_upperband'] = min(df['upperband'].iloc[i], df['final_upperband'].iloc[i-1])
        if df['close'].iloc[i-1] >= df['final_lowerband'].iloc[i-1]:
            df.loc[df.index[i], 'final_lowerband'] = max(df['lowerband'].iloc[i], df['final_lowerband'].iloc[i-1])

    df['supertrend'] = True
    for i in range(1, len(df)):
        if df['close'].iloc[i] <= df['final_lowerband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = False
        elif df['close'].iloc[i] >= df['final_upperband'].iloc[i]:
            df.loc[df.index[i], 'supertrend'] = True
        else:
            df.loc[df.index[i], 'supertrend'] = df['supertrend'].iloc[i-1]

    df['ema300'] = df['close'].ewm(span=EMA_PERIOD, adjust=False).mean()
    return df

def get_klines(symbol, interval='5', limit=350):
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        'category': 'linear',
        'symbol': symbol,
        'interval': interval,
        'limit': limit
    }
    try:
        res = requests.get(url, params=params, timeout=15).json()
        if res['retCode'] == 0:
            data = res['result']['list']
            df = pd.DataFrame(data, columns=['timestamp','open','high','low','close','volume','turnover'])
            df = df.astype({'timestamp': 'int64', 'open': float, 'high': float, 'low': float, 'close': float})
            df = df.iloc[::-1].reset_index(drop=True)
            return df
    except Exception as e:
        print(f"Kline Error {symbol}: {e}", flush=True)
    return None

def bot1_scan_bybit_futures():
    print("Bot1 Bybit Futures thread started", flush=True)
    while True:
        try:
            url = "https://api.bybit.com/v5/market/tickers"
            params = {'category': 'linear'}
            headers = {'User-Agent': 'Mozilla/5.0'}
            response = requests.get(url, params=params, headers=headers, timeout=20)
            data = response.json()

            if data['retCode']!= 0:
                print(f"Bot1 API Error: {data}", flush=True)
                time.sleep(60)
                continue

            tickers = data['result']['list']
            cdcx_count = 0

            for ticker in tickers:
                symbol = ticker['symbol']

                # CoinDCX Filter
                if symbol not in COINDX_FUTURES:
                    continue

                cdcx_count += 1
                change_24h = float(ticker['price24hPcnt']) * 100

                # YAHI MAIN CHANGE HAI: 24h Change >= 40%
                if symbol not in WATCHLIST and change_24h >= PUMP_PERCENT_24H:
                    WATCHLIST[symbol] = {
                        'time': time.time(),
                        'last_st': None
                    }
                    save_watchlist() # FIX 1: Save kar de turant
                    cdcx_name = symbol.replace('USDT', '-USDT')
                    price = ticker['lastPrice']
                    msg = f"🚨 <b>BOT 1: 24H PUMP ALERT</b> 🚨\n\n" \
                          f"<b>Coin:</b> {cdcx_name}\n" \
                          f"<b>24h Change:</b> {change_24h:.2f}%\n" \
                          f"<b>Price:</b> ${price}\n" \
                          f"<b>Exchange:</b> CoinDCX Listed\n" \
                          f"<b>Source:</b> Bybit Futures\n\n" \
                          f"Added to Bot2 watchlist for {WATCHLIST_DAYS} days."
                    send_telegram(msg)
                    print(f"Bot1 Alert: {cdcx_name} {change_24h:.2f}%", flush=True)

            print(f"Bot1: Checked {cdcx_count} CoinDCX pairs out of {len(tickers)} total", flush=True)

        except Exception as e:
            print(f"Bot1 Error: {e}", flush=True)

        time.sleep(300) # Har 5 min me check

def bot2_supertrend_short():
    print("Bot2 Supertrend SHORT thread started", flush=True)
    while True:
        try:
            if not WATCHLIST:
                time.sleep(60)
                continue

            print(f"Bot2: Monitoring {len(WATCHLIST)} coins for SHORT signal", flush=True)
            to_remove = []

            for symbol, info in list(WATCHLIST.items()):
                if time.time() - info['time'] > WATCHLIST_DAYS * 86400:
                    to_remove.append(symbol)
                    continue

                df = get_klines(symbol)
                if df is None or len(df) < EMA_PERIOD + 2:
                    continue

                # ===== FIX 2: CANDLE CLOSE CHECK =====
                last_candle_time = df['timestamp'].iloc[-1]
                # Agar candle abhi close nahi hui - 5min = 300000ms
                if time.time() * 1000 - last_candle_time < 295000: # 5sec buffer
                    print(f"Bot2: Skipping {symbol}, candle not closed yet", flush=True)
                    continue
                # =====================================

                df = calculate_supertrend(df, ATR_PERIOD, ATR_MULTIPLIER)

                # Current values
                st_value = df['final_upperband'].iloc[-1] if not df['supertrend'].iloc[-1] else df['final_lowerband'].iloc[-1]
                st_value_prev = df['final_upperband'].iloc[-2] if not df['supertrend'].iloc[-2] else df['final_lowerband'].iloc[-2]

                ema300 = df['ema300'].iloc[-1]
                ema300_prev = df['ema300'].iloc[-2]
                is_st_red = not df['supertrend'].iloc[-1] # False = Red

                # EXACT CROSS: ST ne EMA300 ko upar se neeche kata + ST Red
                st_crossed_below_ema = st_value < ema300 and st_value_prev > ema300_prev

                cdcx_name = symbol.replace('USDT', '-USDT')

                if st_crossed_below_ema and is_st_red:
                    if info.get('last_st')!= 'short':
                        msg = f"🔻 <b>BOT 2: SHORT SIGNAL</b> 🔻\n\n" \
                              f"<b>Coin:</b> {cdcx_name}\n" \
                              f"<b>Setup:</b> Supertrend(10,3) crossed BELOW EMA(300)\n" \
                              f"<b>Timeframe:</b> 5min\n" \
                              f"<b>ST Value:</b> ${st_value:.6f}\n" \
                              f"<b>EMA300:</b> ${ema300:.6f}\n" \
                              f"<b>Price:</b> ${df['close'].iloc[-1]:.6f}\n\n" \
                              f"CoinDCX Futures pe SHORT entry zone.\n" \
                              f"SL: ${st_value:.6f} ke upar ya recent high"
                        send_telegram(msg)
                        WATCHLIST[symbol]['last_st'] = 'short'
                        save_watchlist() # FIX 1: Update ke baad save
                        print(f"Bot2 SHORT Cross Alert: {cdcx_name}", flush=True)
                else:
                    if info.get('last_st')!= 'long':
                        WATCHLIST[symbol]['last_st'] = 'long'
                        save_watchlist() # FIX 1: Save

                time.sleep(1)

            for symbol in to_remove:
                WATCHLIST.pop(symbol, None)
                save_watchlist() # FIX 1: Remove ke baad save

        except Exception as e:
            print(f"Bot2 Error: {e}", flush=True)

        time.sleep(120) # 2 min me check

@app.route('/')
def home():
    return f"Bot running. Watchlist: {len(WATCHLIST)} coins. CoinDCX Filter: ON"

if __name__ == '__main__':
    print(f"BOT_TOKEN exists: {bool(TELEGRAM_BOT_TOKEN)}", flush=True)
    print(f"CHAT_ID exists: {bool(TELEGRAM_CHAT_ID)}", flush=True)
    print(f"CoinDCX Futures loaded: {len(COINDX_FUTURES)} pairs", flush=True)

    load_watchlist() # FIX 1: Start me load kar

    threading.Thread(target=bot1_scan_bybit_futures, daemon=True).start()
    threading.Thread(target=bot2_supertrend_short, daemon=True).start()
    app.run(host='0.0.0.0', port=10000)
