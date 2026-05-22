import asyncio
import aiohttp
from datetime import datetime, timezone
import time

# =============== EDIT THESE CREDENTIALS ===============
TELEGRAM_BOT_TOKEN = "8261353471:AAEWByECnKyV7FNNQNe4flU2pEfRYfsJi5M"
TELEGRAM_CHAT_ID = "6620972737"
# =====================================================

API_TICKERS = "https://public.coindcx.com/market_data/v3/current_prices/futures/rt"
ALERT_THRESHOLD = 40.0
SCAN_INTERVAL = 300
ALERT_COOLDOWN = 21600

class AlertBot:
    def __init__(self):
        self.session = None
        self.last_alerted = {}

    async def send_telegram(self, text: str):
        if "YOUR_BOT_TOKEN" in TELEGRAM_BOT_TOKEN:
            print("TELEGRAM NOT SETUP - Message:", text)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }
        try:
            async with self.session.post(url, json=payload, timeout=10) as r:
                if r.status!= 200:
                    print(f"Telegram Error {r.status}: {await r.text()}")
        except Exception as e:
            print(f"Failed to send Telegram: {e}")

    async def fetch_perps(self):
        async with self.session.get(API_TICKERS, timeout=15) as r:
            r.raise_for_status()
            data = await r.json()
            # FIX: سارا data 'prices' key کے اندر ہے
            return data.get('prices', {})

    async def scan(self):
        try:
            tickers = await self.fetch_perps()
            now = time.time()
            ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            print(f"[{ts}] Scanning {len(tickers)} perpetual markets...")

            if len(tickers) == 0:
                print("WARNING: Still 0 pairs found in 'prices'")
                return

            for symbol, t in tickers.items():
                if not symbol.startswith('B-'):
                    continue

                coin = symbol.replace('B-', '').replace('_USDT', '')
                change_24h = float(t.get('pc', 0)) # pc = price change %
                price = float(t.get('ls', 0)) # ls = last price

                if change_24h >= ALERT_THRESHOLD:
                    last_time = self.last_alerted.get(symbol, 0)
                    if now - last_time > ALERT_COOLDOWN:
                        msg = (
                            f"🚀 *40%+ PUMP ALERT*\n"
                            f"Coin: `{coin}`\n"
                            f"Signal: *24H UP MOVE*\n"
                            f"24h Change: *+{change_24h:.2f}%*\n"
                            f"Price: `${price:,.4f}`\n"
                            f"Time: `{ts}`"
                        )
                        await self.send_telegram(msg)
                        self.last_alerted[symbol] = now
                        print(f"Alert sent for {coin} +{change_24h:.2f}%")

        except Exception as e:
            print(f"Scan error: {e}")

    async def run(self):
        self.session = aiohttp.ClientSession()
        print("="*60)
        print("CoinDCX Perpetual Pump Monitor")
        print("="*60)
        print(f"INFO - Telegram bot: @mdyk_bot")
        print("✅ Monitor started! Press Ctrl+C to stop.")
        print(f"INFO - Threshold: +{ALERT_THRESHOLD}% | Interval: {SCAN_INTERVAL}s")
        try:
            while True:
                await self.scan()
                await asyncio.sleep(SCAN_INTERVAL)
        except KeyboardInterrupt:
            print("\nStopping bot...")
        finally:
            await self.session.close()

if __name__ == "__main__":
    asyncio.run(AlertBot().run())
