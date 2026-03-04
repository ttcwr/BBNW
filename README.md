# BB NEWSWIRE — Backend Setup

## You need
- Python 3.10+ (you almost certainly have this)
- 2 minutes

---

## Step 1 — Install dependencies (once)

```bash
pip install aiohttp feedparser
```

---

## Step 2 — Put both files in the same folder

```
your-folder/
  server.py
  bb-newswire-dashboard.html
```

---

## Step 3 — Run the server

```bash
python server.py
```

You'll see:
```
╔══════════════════════════════════════════╗
║   BB NEWSWIRE — Backend Server           ║
║   http://localhost:8000                  ║
...
```

---

## Step 4 — Open the dashboard

Either:
- Go to **http://localhost:8000** in your browser
- Or open `bb-newswire-dashboard.html` directly (both work)

The dashboard will show **LIVE** in the top bar and headlines
will start appearing within a few seconds.

---

## Optional — Telegram Push

To enable the Push button:

1. Create a Telegram bot via @BotFather — takes 1 minute
2. Open `server.py` and fill in:
   ```python
   TELEGRAM_BOT_TOKEN = "your-bot-token-here"
   TELEGRAM_CHANNEL   = "@yourchannel"
   ```
3. Restart the server

---

## What feeds are included

| Source | Category |
|--------|----------|
| CoinTelegraph | Media |
| Decrypt | Media |
| CoinDesk | Media |
| Bitcoinist | Media |
| NewsBTC | Media |
| BeInCrypto | Media |
| CryptoPotato | Media |
| AMBCrypto | Media |
| U.Today | Media |
| Bitcoin Magazine | Media |
| The Block | Media |
| Reuters | Wire |
| Glassnode | Onchain |

Feeds refresh every **4 minutes** automatically.

---

## Troubleshooting

**"No module named aiohttp"**
→ Run `pip install aiohttp feedparser`

**Dashboard shows DEMO not LIVE**
→ Make sure server.py is running and you're on http://localhost:8000

**Some feeds show 0 items**
→ Normal — some sites block scrapers. The other 10+ feeds still work.
