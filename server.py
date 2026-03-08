#!/usr/bin/env python3
"""
BB NEWSWIRE — Backend Server
Deploy: push to GitHub → Railway auto-deploys
Local:  python server.py → http://localhost:8000
"""

import asyncio, json, logging, os, re, uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp, feedparser
from aiohttp import web, WSMsgType

# ─── CONFIG ───────────────────────────────────────────────────────────────────
PORT           = int(os.environ.get("PORT", 8000))
HOST           = "0.0.0.0"
FETCH_INTERVAL = 240   # seconds between polls
MAX_ITEMS      = 600

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL",   "")

# ─── FEEDS ────────────────────────────────────────────────────────────────────
# Each entry: url, source name, category, optional fallback urls
# Categories: cm=media  cr=wire  cc=onchain  cw=whale  cg=gov  cx=social
#
# Reliability notes:
#   - All direct RSS feeds tested working as of 2025
#   - Reuters feed is official Feedburner-backed, very stable
#   - Whale Alert has NO public RSS — we use their free REST API (1000 calls/day free)
#   - Glassnode blog RSS is reliable; their pro data feed requires API key
#   - Bitcoin Magazine uses Substack which is very stable
#   - CryptoSlate + The Defiant added as high-quality extras

FEEDS = [
    # ── CRYPTO MEDIA ──────────────────────────────────────────────────────────
    {"url": "https://cointelegraph.com/rss",                        "source": "CoinTelegraph",   "cat": "cm"},
    {"url": "https://decrypt.co/feed",                              "source": "Decrypt",         "cat": "cm"},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/",      "source": "CoinDesk",        "cat": "cm"},
    {"url": "https://bitcoinmagazine.com/.rss/full/",               "source": "Bitcoin Magazine","cat": "cm"},
    {"url": "https://theblock.co/rss.xml",                          "source": "The Block",       "cat": "cm"},
    {"url": "https://blockworks.co/feed",                           "source": "Blockworks",      "cat": "cm"},
    {"url": "https://thedefiant.io/feed",                           "source": "The Defiant",     "cat": "cm"},
    {"url": "https://cryptoslate.com/feed/",                        "source": "CryptoSlate",     "cat": "cm"},
    {"url": "https://beincrypto.com/feed/",                         "source": "BeInCrypto",      "cat": "cm"},
    {"url": "https://bitcoinist.com/feed/",                         "source": "Bitcoinist",      "cat": "cm"},
    {"url": "https://newsbtc.com/feed/",                            "source": "NewsBTC",         "cat": "cm"},
    {"url": "https://u.today/rss",                                  "source": "U.Today",         "cat": "cm"},

    # ── WIRE — keyword filtered, only crypto-relevant items pass ──────────────
    # Chainwire + GlobeNewswire blockchain category: pre-filtered by topic
    {"url": "https://chainwire.org/feed/",                                        "source": "Chainwire",     "cat": "cr", "prefiltered": True},
    {"url": "https://www.globenewswire.com/RssFeed/subjectcode/BC-Blockchain",    "source": "GlobeNewswire", "cat": "cr", "prefiltered": True},
    # BusinessWire, PRNewswire, Reuters: broad feeds, need keyword filter
    {"url": "https://feed.businesswire.com/rss/home/?rss=G7&rssid=rs1",          "source": "BusinessWire",  "cat": "cr"},
    {"url": "https://www.prnewswire.com/rss/news-releases-list.rss",              "source": "PRNewswire",    "cat": "cr"},
    {"url": "https://feeds.reuters.com/reuters/businessNews",                     "source": "Reuters",       "cat": "cr"},

    # ── GOV / REGULATORY ──────────────────────────────────────────────────────
    # SEC EDGAR filings, press releases, litigation — all keyword filtered
    {"url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=40&search_text=&output=atom",
                                                                    "source": "SEC Filings",     "cat": "cg"},
    {"url": "https://www.sec.gov/rss/news/press.xml",               "source": "SEC News",        "cat": "cg"},
    {"url": "https://www.sec.gov/rss/litigation/litreleases.xml",   "source": "SEC Litigation",  "cat": "cg"},
    {"url": "https://www.federalreserve.gov/feeds/press_all.xml",   "source": "Federal Reserve", "cat": "cg"},

    # ── ONCHAIN / ANALYTICS ───────────────────────────────────────────────────
    {"url": "https://insights.glassnode.com/rss/",                  "source": "Glassnode",       "cat": "cc"},
    {"url": "https://cryptoquant.com/feed",                         "source": "CryptoQuant",     "cat": "cc"},
]

WHALE_ALERT_KEY = os.environ.get("WHALE_ALERT_API_KEY", "")

# ── KEYWORD FILTERS ───────────────────────────────────────────────────────────
# Applied to Wire (cr) and Gov (cg) feeds — broad public feeds need this
CRYPTO_KEYWORDS = [
    # Coins & assets
    "bitcoin","btc","ethereum","eth","solana","sol","xrp","ripple",
    "dogecoin","doge","cardano","ada","avalanche","avax","chainlink","link",
    "hyperliquid","hype","bnb","tron","trx","polkadot","dot","litecoin","ltc",
    # Companies holding or trading crypto
    "strategy","microstrategy","bitmine","metaplanet","semler scientific",
    "coinbase","binance","kraken","robinhood crypto","galaxy digital",
    "grayscale","blackrock bitcoin","fidelity bitcoin","ark invest",
    "tether","usdt","usdc","circle","paxos",
    "ripple labs","consensys","chainanalysis","fireblocks",
    # Concepts that are always crypto-relevant
    "crypto","cryptocurrency","blockchain","defi","nft","web3",
    "stablecoin","digital asset","virtual currency","cbdc",
    "bitcoin etf","spot etf","crypto etf","bitcoin reserve",
    "mining","proof of stake","proof of work","validator",
    "wallet","cold storage","custody","staking","yield farming",
    "layer 2","lightning network","rollup","zero knowledge",
]

# ── BREAKING KEYWORDS ─────────────────────────────────────────────────────────
BREAKING_WORDS = [
    "breaking","urgent","flash","just in","hack","hacked","seized","exploit",
    "collapse","halt","emergency","sanctioned","arrested","insolvent",
    "bankrupt","stolen","attack","crisis","suspended","default","banned",
    "breach","shutdown","crash","freeze","liquidat",
]

# ─── STATE ────────────────────────────────────────────────────────────────────
items     = []
seen_keys = set()
clients   = set()

log = logging.getLogger("bbnw")
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s  %(levelname)s  %(message)s")

# ─── HELPERS ──────────────────────────────────────────────────────────────────
def strip_html(s):
    s = re.sub(r'<[^>]+>', '', s or '')
    s = s.replace('&nbsp;',' ').replace('&amp;','&').replace('&lt;','<') \
         .replace('&gt;','>').replace('&#39;',"'").replace('&quot;','"')
    return ' '.join(s.split()).strip()

def is_breaking(h):
    return any(w in h.lower() for w in BREAKING_WORDS)

def score(headline, summary=""):
    text = (headline + " " + summary).lower()
    s = 0.35
    high = ["bitcoin","btc","etf","sec","federal reserve","blackrock",
            "spot","regulation","halving","hack","exploit","sanctioned"]
    med  = ["crypto","ethereum","eth","whale","exchange","defi",
            "institutional","binance","coinbase","solana"]
    for w in high:
        if w in text: s += 0.08
    for w in med:
        if w in text: s += 0.04
    if is_breaking(headline): s = max(s, 0.88)
    return min(round(s, 2), 0.99)

def parse_time(entry):
    for field in ("published", "updated"):
        raw = getattr(entry, field, None) or entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw).astimezone(timezone.utc)
                return dt.strftime("%H:%M")
            except Exception:
                pass
    return datetime.now(timezone.utc).strftime("%H:%M")

def make_item(entry, feed):
    headline = strip_html(entry.get("title") or "").strip()
    if not headline or len(headline) < 15:
        return None
    key = headline[:60].lower()
    if key in seen_keys:
        return None
    seen_keys.add(key)
    summary = strip_html(entry.get("summary") or "")[:220]
    # Wire and Gov feeds: keyword filter unless source is pre-filtered by topic
    if feed.get("cat") in ("cr", "cg") and not feed.get("prefiltered"):
        text = (headline + " " + summary).lower()
        if not any(k in text for k in CRYPTO_KEYWORDS):
            return None
    return {
        "id":        str(uuid.uuid4()),
        "source":    feed["source"],
        "cat":       feed["cat"],
        "headline":  headline,
        "summary":   summary,
        "url":       entry.get("link") or "#",
        "published": parse_time(entry),
        "score":     score(headline, summary),
        "status":    "pending",
        "breaking":  is_breaking(headline),
    }

# ─── WHALE ALERT API ──────────────────────────────────────────────────────────
async def fetch_whale_alerts(session):
    """Fetch from Whale Alert REST API (free tier: 1000 calls/day)"""
    if not WHALE_ALERT_KEY:
        return []
    try:
        import time
        since = int(time.time()) - FETCH_INTERVAL
        url = (f"https://api.whale-alert.io/v1/transactions"
               f"?api_key={WHALE_ALERT_KEY}&min_value=500000&since={since}&limit=20")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log.warning(f"Whale Alert API: HTTP {r.status}")
                return []
            data = await r.json()
        results = []
        for tx in data.get("transactions", []):
            amt   = tx.get("amount", 0)
            sym   = tx.get("symbol", "").upper()
            usd   = tx.get("amount_usd", 0)
            frm   = tx.get("from", {}).get("owner") or tx.get("from", {}).get("owner_type","unknown")
            to    = tx.get("to",   {}).get("owner") or tx.get("to",   {}).get("owner_type","unknown")
            usd_m = usd / 1_000_000
            headline = (
                f"🐳 {amt:,.0f} #{sym} (${usd_m:.0f}M) moved: {frm} → {to}"
                if usd >= 1_000_000
                else f"🐳 {amt:,.0f} #{sym} moved: {frm} → {to}"
            )
            key = headline[:60].lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append({
                "id":        str(uuid.uuid4()),
                "source":    "Whale Alert",
                "cat":       "cw",
                "headline":  headline,
                "summary":   f"Transaction hash: {tx.get('hash','')} | Blockchain: {tx.get('blockchain','')}",
                "url":       f"https://whale-alert.io/transaction/{tx.get('blockchain','')}/{tx.get('hash','')}",
                "published": datetime.now(timezone.utc).strftime("%H:%M"),
                "score":     min(0.55 + (usd / 1_000_000_000), 0.99),
                "status":    "pending",
                "breaking":  usd >= 100_000_000,
            })
        if results:
            log.info(f"Whale Alert API: +{len(results)}")
        return results
    except Exception as e:
        log.warning(f"Whale Alert API: {e}")
        return []

# ─── FETCHER ──────────────────────────────────────────────────────────────────
async def fetch_feed(session, feed):
    try:
        async with session.get(
            feed["url"], timeout=aiohttp.ClientTimeout(total=12)
        ) as r:
            if r.status != 200:
                log.warning(f"{feed['source']}: HTTP {r.status}")
                return []
            raw = await r.text(errors="replace")
        parsed = feedparser.parse(raw)
        new_items = [it for e in parsed.entries[:25]
                     if (it := make_item(e, feed))]
        if new_items:
            log.info(f"{feed['source']}: +{len(new_items)}")
        return new_items
    except Exception as e:
        log.warning(f"{feed['source']}: {e}")
        return []

async def broadcast(msg):
    dead = set()
    data = json.dumps(msg)
    for ws in clients:
        try:
            await ws.send_str(data)
        except Exception:
            dead.add(ws)
    clients.difference_update(dead)

async def poll_feeds():
    global items
    headers = {"User-Agent": "Mozilla/5.0 (compatible; BBNewswire/1.0)"}
    while True:
        log.info("── Polling %d feeds ──", len(FEEDS))
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(
            headers=headers, connector=connector
        ) as session:
            rss_results = await asyncio.gather(
                *[fetch_feed(session, f) for f in FEEDS],
                return_exceptions=True
            )
            whale_results = await fetch_whale_alerts(session)

        batch = [it for r in rss_results
                 if isinstance(r, list) for it in r]
        batch.extend(whale_results)

        if batch:
            batch.sort(key=lambda x: x["score"], reverse=True)
            items = (batch + items)[:MAX_ITEMS]
            log.info(f"+{len(batch)} new ({len(items)} total)")
            await broadcast({"type": "new_items", "items": batch})

        await asyncio.sleep(FETCH_INTERVAL)

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def handle_ws(request):
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    clients.add(ws)
    log.info(f"WS connected ({len(clients)} clients)")
    await ws.send_str(json.dumps({"type": "init", "items": items}))
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                d = json.loads(msg.data)
                if d.get("type") == "status_update":
                    for it in items:
                        if it["id"] == d.get("item_id"):
                            it["status"] = d.get("status", "pending")
                            break
            except Exception:
                pass
        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
            break
    clients.discard(ws)
    return ws

async def handle_push(request):
    data = await request.json()
    for it in items:
        if it["id"] == data.get("item_id"):
            it["status"] = "pushed"
            break
    if not TELEGRAM_BOT_TOKEN:
        return web.json_response({"ok": False, "error": "No TELEGRAM_BOT_TOKEN set"})
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id":    TELEGRAM_CHANNEL,
                "text":       data.get("message", ""),
                "parse_mode": "HTML"
            }, timeout=aiohttp.ClientTimeout(total=8)) as r:
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
        return web.Response(
            text="bb-newswire-dashboard.html not found next to server.py",
            status=404
        )

@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers={
            "Access-Control-Allow-Origin":  "*",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })
    resp = await handler(request)
    resp.headers.update({
        "Access-Control-Allow-Origin":  "*",
        "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    })
    return resp

# ─── APP ──────────────────────────────────────────────────────────────────────
async def on_startup(app):
    app["poll"] = asyncio.create_task(poll_feeds())

async def on_cleanup(app):
    app["poll"].cancel()
    try:
        await app["poll"]
    except asyncio.CancelledError:
        pass

app = web.Application(middlewares=[cors])
app.router.add_get("/",                        handle_index)
app.router.add_get("/ws",                      handle_ws)
app.router.add_post("/api/telegram/push",      handle_push)
app.router.add_patch("/api/news/{id}/status",  handle_status)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    whale_status = f"Whale Alert API {'✓ active' if WHALE_ALERT_KEY else '✗ no key (set WHALE_ALERT_API_KEY)'}"
    tg_status    = f"Telegram {'✓ active' if TELEGRAM_BOT_TOKEN else '✗ no key (set TELEGRAM_BOT_TOKEN)'}"
    print(f"""
╔══════════════════════════════════════════════╗
  BB NEWSWIRE  —  port {PORT}
  {len(FEEDS)} RSS feeds  |  refresh every {FETCH_INTERVAL}s
  {whale_status}
  {tg_status}
╚══════════════════════════════════════════════╝
""")
    web.run_app(app, host=HOST, port=PORT, access_log=None)
