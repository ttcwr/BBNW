#!/usr/bin/env python3
"""
BB NEWSWIRE — Backend Server
"""
import asyncio, json, logging, os, re, uuid, time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import aiohttp, feedparser
from aiohttp import web, WSMsgType

PORT           = int(os.environ.get("PORT", 8000))
HOST           = "0.0.0.0"
FETCH_INTERVAL = 120
MAX_ITEMS      = 600

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL   = os.environ.get("TELEGRAM_CHANNEL",   "")
WHALE_ALERT_KEY    = os.environ.get("WHALE_ALERT_API_KEY", "")

FEEDS = [
    # CRYPTO MEDIA
    {"url": "https://cointelegraph.com/rss",                       "source": "CoinTelegraph",    "cat": "cm"},
    {"url": "https://decrypt.co/feed",                             "source": "Decrypt",          "cat": "cm"},
    {"url": "https://www.coindesk.com/arc/outboundfeeds/rss/",     "source": "CoinDesk",         "cat": "cm"},
    {"url": "https://bitcoinmagazine.com/.rss/full/",              "source": "Bitcoin Magazine", "cat": "cm"},
    {"url": "https://theblock.co/rss.xml",                         "source": "The Block",        "cat": "cm"},
    {"url": "https://blockworks.co/feed",                          "source": "Blockworks",       "cat": "cm"},
    {"url": "https://thedefiant.io/feed",                          "source": "The Defiant",      "cat": "cm"},
    {"url": "https://bitcoinist.com/feed/",                        "source": "Bitcoinist",       "cat": "cm"},
    {"url": "https://newsbtc.com/feed/",                           "source": "NewsBTC",          "cat": "cm"},
    {"url": "https://u.today/rss",                                 "source": "U.Today",          "cat": "cm"},
    {"url": "https://www.forbes.com/digital-assets/feed/",          "source": "Forbes",           "cat": "cm"},
    {"url": "https://cointelegraph.com/rss/tag/analysis",               "source": "CT Analysis",      "cat": "cm"},
    {"url": "https://protos.com/feed/",                             "source": "Protos",           "cat": "cm"},
    # WIRE — prefiltered feeds don't need keyword check
    {"url": "https://chainwire.org/feed/",                                         "source": "Chainwire",     "cat": "cr", "prefiltered": True},
    {"url": "https://www.globenewswire.com/RssFeed/subjectcode/BC-Blockchain",     "source": "GlobeNewswire", "cat": "cr", "prefiltered": True},
    {"url": "https://feed.businesswire.com/rss/home/?rss=G7&rssid=rs1",           "source": "BusinessWire",  "cat": "cr"},
    {"url": "https://www.prnewswire.com/rss/news-releases-list.rss",               "source": "PRNewswire",    "cat": "cr"},
    {"url": "https://feeds.reuters.com/reuters/businessNews",                      "source": "Reuters",       "cat": "cr"},
    # GOV
    {"url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=40&search_text=&output=atom",
                                                                   "source": "SEC Filings",     "cat": "cg"},
    {"url": "https://www.sec.gov/rss/news/press.xml",              "source": "SEC News",        "cat": "cg"},
    {"url": "https://www.sec.gov/rss/litigation/litreleases.xml",  "source": "SEC Litigation",  "cat": "cg"},
    {"url": "https://www.federalreserve.gov/feeds/press_all.xml",  "source": "Federal Reserve", "cat": "cg"},
    # ONCHAIN
    {"url": "https://insights.glassnode.com/rss/",                 "source": "Glassnode",       "cat": "cc"},
    {"url": "https://cryptoquant.com/feed",                        "source": "CryptoQuant",     "cat": "cc"},
]

CRYPTO_KEYWORDS = [
    # Major coins
    "bitcoin","btc","ethereum","eth","solana","sol","xrp","ripple",
    "dogecoin","doge","cardano","ada","avalanche","avax","chainlink","link",
    "hyperliquid","hype","bnb","tron","trx","polkadot","dot","litecoin","ltc",
    "shiba","pepe","sui","aptos","near","atom","cosmos","algo","algorand",
    "matic","polygon","arb","arbitrum","op","optimism","base","sei","ton",
    # Companies
    "strategy","microstrategy","bitmine","metaplanet","semler","mara","riot",
    "coinbase","binance","kraken","robinhood","galaxy digital","cumberland",
    "grayscale","blackrock","fidelity","ark invest","vaneck","bitwise",
    "tether","usdt","usdc","circle","paxos","ripple labs","consensys",
    "coinshares","21shares","proshares","hashdex",
    # Concepts
    "crypto","cryptocurrency","blockchain","defi","nft","web3",
    "stablecoin","digital asset","virtual currency","cbdc","tokeniz",
    "bitcoin etf","spot etf","crypto etf","bitcoin reserve","strategic reserve",
    "mining","proof of stake","proof of work","validator","miner",
    "wallet","cold storage","custody","staking","yield","airdrop",
    "layer 2","lightning network","rollup","zero knowledge","zk-proof",
    "dex","exchange","swap","liquidity","amm","protocol",
    "halving","bull","bear","market","price","rally","dump","crash",
    "sec","cftc","regulation","regulatory","compliance","enforcement",
    "hack","exploit","breach","stolen","rug pull","scam","fraud",
    "institutional","fund","etf","futures","options","derivatives",
    "whale","on-chain","onchain","transaction","transfer","volume",
    "treasury","reserve","holding","invest","acquisition",
]

BREAKING_WORDS = [
    "breaking","urgent","flash","just in","hack","hacked","seized","exploit",
    "collapse","halt","emergency","sanctioned","arrested","insolvent",
    "bankrupt","stolen","attack","crisis","suspended","default","banned",
    "breach","shutdown","crash","freeze","liquidat",
]

items     = []
seen_keys = set()
clients   = set()

log = logging.getLogger("bbnw")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")

def strip_html(s):
    s = re.sub(r'<[^>]+>', '', s or '')
    s = s.replace('&nbsp;',' ').replace('&amp;','&').replace('&lt;','<') \
         .replace('&gt;','>').replace('&#39;',"'").replace('&quot;','"')
    return ' '.join(s.split()).strip()

def is_breaking(h):
    return any(w in h.lower() for w in BREAKING_WORDS)

def calc_score(headline, summary=""):
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
    """Returns (iso_string, hhmm_string) for sorting and display."""
    for field in ("published", "updated"):
        raw = getattr(entry, field, None) or entry.get(field)
        if raw:
            try:
                dt = parsedate_to_datetime(raw).astimezone(timezone.utc)
                return dt.isoformat(), dt.strftime("%H:%M")
            except Exception:
                pass
    now = datetime.now(timezone.utc)
    return now.isoformat(), now.strftime("%H:%M")

def make_item(entry, feed):
    headline = strip_html(entry.get("title") or "").strip()
    if not headline or len(headline) < 15:
        return None
    key = headline[:60].lower()
    if key in seen_keys:
        return None
    seen_keys.add(key)
    summary = strip_html(entry.get("summary") or "")[:220]
    # Wire/Gov: keyword filter unless prefiltered
    # Media (cm) feeds are already crypto-specific — no filtering needed
    if feed.get("cat") in ("cr", "cg") and not feed.get("prefiltered"):
        text = (headline + " " + summary).lower()
        if not any(k in text for k in CRYPTO_KEYWORDS):
            return None
    iso, hhmm = parse_time(entry)
    return {
        "id":        str(uuid.uuid4()),
        "source":    feed["source"],
        "cat":       feed["cat"],
        "headline":  headline,
        "summary":   summary,
        "url":       entry.get("link") or "#",
        "published": iso,    # ISO string for proper sorting
        "time_hhmm": hhmm,   # display only
        "score":     calc_score(headline, summary),
        "status":    "pending",
        "breaking":  is_breaking(headline),
    }

async def fetch_whale_alerts(session):
    if not WHALE_ALERT_KEY:
        return []
    try:
        since = int(time.time()) - FETCH_INTERVAL
        url = (f"https://api.whale-alert.io/v1/transactions"
               f"?api_key={WHALE_ALERT_KEY}&min_value=500000&since={since}&limit=20")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                return []
            data = await r.json()
        results = []
        now = datetime.now(timezone.utc)
        for tx in data.get("transactions", []):
            amt  = tx.get("amount", 0)
            sym  = tx.get("symbol", "").upper()
            usd  = tx.get("amount_usd", 0)
            frm  = tx.get("from", {}).get("owner") or tx.get("from", {}).get("owner_type","unknown")
            to   = tx.get("to",   {}).get("owner") or tx.get("to",   {}).get("owner_type","unknown")
            usd_m = usd / 1_000_000
            headline = (f"🐳 {amt:,.0f} #{sym} (${usd_m:.0f}M) moved: {frm} → {to}"
                        if usd >= 1_000_000
                        else f"🐳 {amt:,.0f} #{sym} moved: {frm} → {to}")
            key = headline[:60].lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append({
                "id":        str(uuid.uuid4()),
                "source":    "Whale Alert",
                "cat":       "cw",
                "headline":  headline,
                "summary":   f"Hash: {tx.get('hash','')} | Chain: {tx.get('blockchain','')}",
                "url":       f"https://whale-alert.io/transaction/{tx.get('blockchain','')}/{tx.get('hash','')}",
                "published": now.isoformat(),
                "time_hhmm": now.strftime("%H:%M"),
                "score":     min(0.55 + (usd / 1_000_000_000), 0.99),
                "status":    "pending",
                "breaking":  usd >= 100_000_000,
            })
        return results
    except Exception as e:
        log.warning(f"Whale Alert: {e}")
        return []

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Feedfetcher-Google; (+http://www.google.com/feedfetcher.html)",
    "Mozilla/5.0 (compatible; NewsBot/1.0; +https://newsbot.example.com)",
]
import random

async def fetch_feed(session, feed):
    for attempt in range(2):
        try:
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
            }
            timeout = aiohttp.ClientTimeout(total=20, connect=8)
            async with session.get(feed["url"], timeout=timeout, headers=headers, allow_redirects=True) as r:
                if r.status == 429:
                    log.warning(f"{feed['source']}: rate limited")
                    return []
                if r.status not in (200, 301, 302):
                    log.warning(f"{feed['source']}: HTTP {r.status}")
                    return []
                raw = await r.text(errors="replace")
            parsed = feedparser.parse(raw)
            if not parsed.entries:
                log.warning(f"{feed['source']}: 0 entries parsed (bozo={parsed.get('bozo',False)})")
                return []
            new_items = [it for e in parsed.entries[:50] if (it := make_item(e, feed))]
            if new_items:
                log.info(f"{feed['source']}: +{len(new_items)} new / {len(parsed.entries)} total")
            return new_items
        except asyncio.TimeoutError:
            log.warning(f"{feed['source']}: timeout (attempt {attempt+1})")
            if attempt == 0:
                await asyncio.sleep(2)
        except Exception as e:
            log.warning(f"{feed['source']}: {e}")
            return []
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
    poll_count = 0
    while True:
        poll_count += 1
        # Clear seen_keys every 12 polls (~24 min) to allow re-fetching updated stories
        if poll_count % 12 == 0:
            old_size = len(seen_keys)
            # Keep only keys from current items
            current_keys = {it["headline"][:60].lower() for it in items}
            seen_keys.intersection_update(current_keys)
            log.info(f"Cleared {old_size - len(seen_keys)} stale seen_keys")
        log.info("── Polling %d feeds ──", len(FEEDS))
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            rss_results = await asyncio.gather(
                *[fetch_feed(session, f) for f in FEEDS],
                return_exceptions=True
            )
            whale_results = await fetch_whale_alerts(session)

        batch = [it for r in rss_results if isinstance(r, list) for it in r]
        batch.extend(whale_results)

        if batch:
            items = (batch + items)[:MAX_ITEMS]
            # Sort by ISO timestamp descending — proper chronological order
            items.sort(key=lambda x: x.get("published", ""), reverse=True)
            cats = {}
            for it in batch:
                cats[it['cat']] = cats.get(it['cat'], 0) + 1
            log.info(f"+{len(batch)} new | {len(items)} total | {cats}")
            await broadcast({"type": "new_items", "items": batch})
        else:
            log.info("No new items this poll")

        await asyncio.sleep(FETCH_INTERVAL)

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

async def handle_debug(request):
    cats = {}
    sources = {}
    for it in items:
        cats[it.get("cat","?")] = cats.get(it.get("cat","?"), 0) + 1
        sources[it.get("source","?")] = sources.get(it.get("source","?"), 0) + 1
    return web.json_response({
        "total": len(items),
        "by_cat": cats,
        "by_source": sources,
        "newest_5": [{"src": i["source"], "cat": i["cat"], "pub": i["published"], "hl": i["headline"][:50]} for i in items[:5]],
        "sample_cr": [i["headline"][:60] for i in items if i.get("cat")=="cr"][:5],
        "sample_cg": [i["headline"][:60] for i in items if i.get("cat")=="cg"][:5],
    })

async def handle_push(request):
    data = await request.json()
    for it in items:
        if it["id"] == data.get("item_id"):
            it["status"] = "pushed"
            break
    if not TELEGRAM_BOT_TOKEN:
        return web.json_response({"ok": False, "error": "No TELEGRAM_BOT_TOKEN"})
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        async with aiohttp.ClientSession() as s:
            async with s.post(url, json={
                "chat_id": TELEGRAM_CHANNEL,
                "text": data.get("message", ""),
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
        return web.Response(text="bb-newswire-dashboard.html not found", status=404)

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

async def on_startup(app):
    app["poll"] = asyncio.create_task(poll_feeds())

async def on_cleanup(app):
    app["poll"].cancel()
    try:
        await app["poll"]
    except asyncio.CancelledError:
        pass

app = web.Application(middlewares=[cors])
app.router.add_get("/",                       handle_index)
app.router.add_get("/ws",                     handle_ws)
app.router.add_get("/debug",                  handle_debug)
app.router.add_post("/api/telegram/push",     handle_push)
app.router.add_patch("/api/news/{id}/status", handle_status)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    print(f"BB NEWSWIRE — port {PORT} | {len(FEEDS)} feeds | Whale Alert: {'ON' if WHALE_ALERT_KEY else 'OFF'}")
    web.run_app(app, host=HOST, port=PORT, access_log=None)
