#!/usr/bin/env python3
"""
BB NEWSWIRE — Backend Server
Deploy to Railway: just push this folder, Railway auto-detects Python.
Local: python server.py  →  http://localhost:8000
"""

import asyncio, json, logging, os, uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp, feedparser
from aiohttp import web, WSMsgType

PORT  = int(os.environ.get("PORT", 8000))
HOST  = "0.0.0.0"
FETCH_INTERVAL = 240
MAX_ITEMS      = 500

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL",   "")

FEEDS = [
    {"url": "https://cointelegraph.com/rss",                    "source": "CoinTelegraph",   "cat": "cm"},
    {"url": "https://decrypt.co/feed",                          "source": "Decrypt",         "cat": "cm"},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/",  "source": "CoinDesk",        "cat": "cm"},
    {"url": "https://bitcoinist.com/feed/",                     "source": "Bitcoinist",      "cat": "cm"},
    {"url": "https://newsbtc.com/feed/",                        "source": "NewsBTC",         "cat": "cm"},
    {"url": "https://beincrypto.com/feed/",                     "source": "BeInCrypto",      "cat": "cm"},
    {"url": "https://cryptopotato.com/feed/",                   "source": "CryptoPotato",    "cat": "cm"},
    {"url": "https://ambcrypto.com/feed/",                      "source": "AMBCrypto",       "cat": "cm"},
    {"url": "https://u.today/rss",                              "source": "U.Today",         "cat": "cm"},
    {"url": "https://bitcoinmagazine.com/.rss/full/",           "source": "Bitcoin Magazine","cat": "cm"},
    {"url": "https://theblock.co/rss.xml",                      "source": "The Block",       "cat": "cm"},
    {"url": "https://feeds.reuters.com/reuters/businessNews",   "source": "Reuters",         "cat": "cr"},
    {"url": "https://insights.glassnode.com/rss/",              "source": "Glassnode",       "cat": "cc"},
]

BREAKING_WORDS = ["breaking","urgent","flash","just in","hack","hacked","seized","exploit",
    "collapse","halt","emergency","sanctioned","arrested","insolvent",
    "bankrupt","stolen","attack","crisis","suspended","default","banned"]

items = []; seen_keys = set(); clients = set()
log = logging.getLogger("bbnw")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

def is_breaking(h): return any(w in h.lower() for w in BREAKING_WORDS)

def score(headline, summary=""):
    text = (headline + " " + summary).lower()
    s = 0.35
    for w in ["bitcoin","btc","etf","sec","federal reserve","blackrock","spot","regulation","halving"]: 
        if w in text: s += 0.08
    for w in ["crypto","ethereum","eth","whale","exchange","defi","institutional"]: 
        if w in text: s += 0.04
    if is_breaking(headline): s = max(s, 0.88)
    return min(round(s, 2), 0.99)

def parse_time(entry):
    for field in ("published", "updated"):
        raw = getattr(entry, field, None) or entry.get(field)
        if raw:
            try: return parsedate_to_datetime(raw).astimezone(timezone.utc).strftime("%H:%M")
            except: pass
    return datetime.now(timezone.utc).strftime("%H:%M")

def make_item(entry, feed):
    headline = (entry.get("title") or "").strip()
    if not headline or len(headline) < 15: return None
    key = headline[:60].lower()
    if key in seen_keys: return None
    seen_keys.add(key)
    summary = (entry.get("summary") or "")[:300]
    return {"id": str(uuid.uuid4()), "source": feed["source"], "cat": feed["cat"],
            "headline": headline, "summary": summary, "url": entry.get("link") or "#",
            "published": parse_time(entry), "score": score(headline, summary),
            "status": "pending", "breaking": is_breaking(headline)}

async def fetch_feed(session, feed):
    try:
        async with session.get(feed["url"], timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200: return []
            raw = await r.text(errors="replace")
        parsed = feedparser.parse(raw)
        new_items = [it for e in parsed.entries[:20] if (it := make_item(e, feed))]
        if new_items: log.info(f"{feed['source']}: +{len(new_items)}")
        return new_items
    except Exception as e:
        log.warning(f"{feed['source']}: {e}"); return []

async def broadcast(msg):
    dead = set(); data = json.dumps(msg)
    for ws in clients:
        try: await ws.send_str(data)
        except: dead.add(ws)
    clients.difference_update(dead)

async def poll_feeds():
    global items
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BBNewswire/1.0)"}
    while True:
        log.info("── Polling %d feeds ──", len(FEEDS))
        async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(ssl=False)) as session:
            results = await asyncio.gather(*[fetch_feed(session, f) for f in FEEDS], return_exceptions=True)
        batch = [it for r in results if isinstance(r, list) for it in r]
        if batch:
            batch.sort(key=lambda x: x["score"], reverse=True)
            items = (batch + items)[:MAX_ITEMS]
            log.info(f"+{len(batch)} new ({len(items)} total)")
            await broadcast({"type": "new_items", "items": batch})
        await asyncio.sleep(FETCH_INTERVAL)

async def handle_ws(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request); clients.add(ws)
    log.info(f"WS connected ({len(clients)} clients)")
    await ws.send_str(json.dumps({"type": "init", "items": items}))
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                d = json.loads(msg.data)
                if d.get("type") == "status_update":
                    for it in items:
                        if it["id"] == d.get("item_id"): it["status"] = d.get("status"); break
            except: pass
        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE): break
    clients.discard(ws); return ws

async def handle_push(request):
    data = await request.json()
    for it in items:
        if it["id"] == data.get("item_id"): it["status"] = "pushed"; break
    if not TELEGRAM_BOT_TOKEN:
        return web.json_response({"ok": False, "error": "Telegram not configured"})
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHANNEL, "text": data.get("message",""), "parse_mode":"HTML"},
                timeout=aiohttp.ClientTimeout(total=8)) as r:
                result = await r.json()
        return web.json_response({"ok": result.get("ok", False)})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

async def handle_status(request):
    for it in items:
        if it["id"] == request.match_info["id"]:
            it["status"] = (await request.json()).get("status", "pending")
            return web.json_response({"ok": True})
    return web.json_response({"ok": False}, status=404)

async def handle_index(request):
    try:
        with open("bb-newswire-dashboard.html") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(text="bb-newswire-dashboard.html not found next to server.py", status=404)

@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET,POST,PATCH,OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
    resp = await handler(request)
    resp.headers.update({"Access-Control-Allow-Origin":"*","Access-Control-Allow-Methods":"GET,POST,PATCH,OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
    return resp

async def on_startup(app): app["poll"] = asyncio.create_task(poll_feeds())
async def on_cleanup(app):
    app["poll"].cancel()
    try: await app["poll"]
    except asyncio.CancelledError: pass

app = web.Application(middlewares=[cors])
app.router.add_get("/",  handle_index)
app.router.add_get("/ws", handle_ws)
app.router.add_post("/api/telegram/push", handle_push)
app.router.add_patch("/api/news/{id}/status", handle_status)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    print(f"\n{'='*44}\n  BB NEWSWIRE  —  port {PORT}\n  {len(FEEDS)} feeds, refresh every {FETCH_INTERVAL}s\n{'='*44}\n")
    web.run_app(app, host=HOST, port=PORT, access_log=None)
