#!/usr/bin/env python3
"""
BB NEWSWIRE — X (Twitter) Bot
Posts breaking crypto news, wire releases, and price alerts automatically.

Setup:
  Set these Railway environment variables:
    X_API_KEY
    X_API_SECRET
    X_ACCESS_TOKEN
    X_ACCESS_TOKEN_SECRET

Deploy: already included in your Railway server — starts alongside server.py
"""

import asyncio, logging, os, time, hmac, hashlib, base64, urllib.parse, json
import aiohttp
from datetime import datetime, timezone

log = logging.getLogger("xbot")

# ── Config ──
X_API_KEY            = os.environ.get("X_API_KEY", "")
X_API_SECRET         = os.environ.get("X_API_SECRET", "")
X_ACCESS_TOKEN       = os.environ.get("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET= os.environ.get("X_ACCESS_TOKEN_SECRET", "")

MIN_SCORE      = 0.80   # Only post items with score >= 80%
POST_INTERVAL  = 900    # Min seconds between posts (15 min) — avoid spam
PRICE_ALERT_PCT= 3.0    # Alert when BTC/ETH moves this % in 1 hour
MAX_POSTS_DAY  = 40     # Hard cap — free tier is 1500/month

TWEET_ENDPOINT = "https://api.twitter.com/2/tweets"
PRICE_URL_BTC  = "https://api.coinbase.com/v2/prices/BTC-USD/spot"
PRICE_URL_ETH  = "https://api.coinbase.com/v2/prices/ETH-USD/spot"

# ── State ──
posted_ids     = set()   # prevent duplicate posts
last_post_time = 0
posts_today    = 0
day_reset_ts   = 0
btc_price_1h   = None
eth_price_1h   = None
btc_last_alert = 0
eth_last_alert = 0

def enabled():
    return all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET])

# ── OAuth 1.0a signing ──
def oauth_sign(method, url, params, body_params=None):
    """Generate OAuth 1.0a Authorization header."""
    ts    = str(int(time.time()))
    nonce = base64.b64encode(os.urandom(16)).decode().strip("=+/")

    oauth_params = {
        "oauth_consumer_key":     X_API_KEY,
        "oauth_nonce":            nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp":        ts,
        "oauth_token":            X_ACCESS_TOKEN,
        "oauth_version":          "1.0",
    }

    all_params = {**params, **oauth_params}
    if body_params:
        all_params.update(body_params)

    sorted_params = "&".join(
        f"{urllib.parse.quote(k,'')  }={urllib.parse.quote(str(v),'')}"
        for k, v in sorted(all_params.items())
    )
    base = "&".join([
        urllib.parse.quote(method.upper(), ""),
        urllib.parse.quote(url, ""),
        urllib.parse.quote(sorted_params, ""),
    ])
    signing_key = f"{urllib.parse.quote(X_API_SECRET,''  )}&{urllib.parse.quote(X_ACCESS_TOKEN_SECRET,'')}"
    sig = base64.b64encode(
        hmac.new(signing_key.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()

    oauth_params["oauth_signature"] = sig
    header = "OAuth " + ", ".join(
        f'{urllib.parse.quote(k,"")}="{urllib.parse.quote(str(v),"")}"'
        for k, v in sorted(oauth_params.items())
    )
    return header

async def post_tweet(session, text):
    """Post a tweet via X API v2."""
    global last_post_time, posts_today, day_reset_ts

    if not enabled():
        log.info(f"[XBOT] X API not configured — would post: {text[:80]}")
        return False

    # Daily cap
    now = time.time()
    if now - day_reset_ts > 86400:
        posts_today = 0
        day_reset_ts = now
    if posts_today >= MAX_POSTS_DAY:
        log.warning("[XBOT] Daily post cap reached")
        return False

    # Rate limit
    if now - last_post_time < POST_INTERVAL:
        log.debug(f"[XBOT] Rate limited — {int(POST_INTERVAL-(now-last_post_time))}s remaining")
        return False

    body = {"text": text}
    body_json = json.dumps(body)

    auth_header = oauth_sign("POST", TWEET_ENDPOINT, {})

    try:
        async with session.post(
            TWEET_ENDPOINT,
            data=body_json,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            resp = await r.json()
            if r.status in (200, 201):
                tweet_id = resp.get("data", {}).get("id", "?")
                log.info(f"[XBOT] ✓ Posted tweet {tweet_id}: {text[:60]}")
                last_post_time = now
                posts_today += 1
                return True
            else:
                log.warning(f"[XBOT] Failed {r.status}: {resp}")
                return False
    except Exception as e:
        log.warning(f"[XBOT] Post error: {e}")
        return False

def format_news_tweet(item):
    """Format a news item into a tweet."""
    headline = item.get("headline", "")
    source   = item.get("source", "")
    url      = item.get("url", "")
    cat      = item.get("cat", "cm")
    score    = item.get("score", 0)
    breaking = item.get("breaking", False)

    # Category tags
    cat_tags = {
        "cm": "#Crypto #CryptoNews",
        "cr": "#CryptoNews #PressRelease",
        "cg": "#Regulation #Crypto",
        "cc": "#OnChain #Crypto",
        "cw": "#WhaleAlert #Crypto",
    }
    tags = cat_tags.get(cat, "#Crypto")

    # Build tweet
    prefix = "🚨 BREAKING: " if breaking else "📰 "
    source_label = f"[{source.upper()}]"

    # Trim headline to fit 280 chars
    max_hl = 280 - len(prefix) - len(source_label) - len(tags) - len(url) - 10
    hl = headline[:max_hl] + ("…" if len(headline) > max_hl else "")

    tweet = f"{prefix}{hl}\n\n{source_label} {tags}"
    if url and url != "#":
        tweet += f"\n{url}"

    return tweet[:280]

def format_price_alert(coin, price, change_pct):
    """Format a price alert tweet."""
    direction = "🟢" if change_pct > 0 else "🔴"
    arrow = "↑" if change_pct > 0 else "↓"
    word = "surges" if change_pct > 0 else "drops"

    tweet = (
        f"{direction} #{coin} {word} {abs(change_pct):.1f}% in the last hour\n\n"
        f"Current price: ${price:,.0f}\n\n"
        f"#Crypto #Bitcoin #CryptoMarkets"
        if coin == "BTC" else
        f"{direction} #{coin} {word} {abs(change_pct):.1f}% in the last hour\n\n"
        f"Current price: ${price:,.2f}\n\n"
        f"#Crypto #Ethereum #CryptoMarkets"
    )
    return tweet[:280]

async def check_prices(session):
    """Check BTC/ETH prices and post alerts on big moves."""
    global btc_price_1h, eth_price_1h, btc_last_alert, eth_last_alert

    now = time.time()

    for coin, url, price_1h_ref, last_alert_ref in [
        ("BTC", PRICE_URL_BTC, btc_price_1h, btc_last_alert),
        ("ETH", PRICE_URL_ETH, eth_price_1h, eth_last_alert),
    ]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status != 200: continue
                d = await r.json()
                price = float(d["data"]["amount"])

            if coin == "BTC":
                if btc_price_1h is None:
                    btc_price_1h = price
                    continue
                ref = btc_price_1h
            else:
                if eth_price_1h is None:
                    eth_price_1h = price
                    continue
                ref = eth_price_1h

            change_pct = ((price - ref) / ref) * 100

            # Alert on big move, max once per 2 hours per coin
            if abs(change_pct) >= PRICE_ALERT_PCT and (now - last_alert_ref) > 7200:
                tweet = format_price_alert(coin, price, change_pct)
                success = await post_tweet(session, tweet)
                if success:
                    if coin == "BTC": btc_last_alert = now
                    else: eth_last_alert = now

            # Update 1h reference every 60 min
            if coin == "BTC":
                btc_price_1h = price
            else:
                eth_price_1h = price

        except Exception as e:
            log.debug(f"[XBOT] Price check {coin}: {e}")

async def process_items(session, items):
    """Check items list and post qualifying ones."""
    for item in items:
        if item.get("id") in posted_ids:
            continue
        score = item.get("score", 0)
        cat   = item.get("cat", "cm")

        # Only post high-score items + wire releases
        qualifies = (
            score >= MIN_SCORE or
            (cat == "cr" and score >= 0.70) or  # Wire: slightly lower threshold
            item.get("breaking", False)
        )

        if not qualifies:
            continue

        tweet = format_news_tweet(item)
        success = await post_tweet(session, tweet)
        if success:
            posted_ids.add(item.get("id"))
            # Don't spam — only one post per cycle
            return

async def run_xbot(get_items_fn):
    """
    Main bot loop. Call this from server.py with a function that returns current items.
    
    Usage in server.py:
        asyncio.create_task(run_xbot(lambda: items))
    """
    if not enabled():
        log.info("[XBOT] X API keys not set — bot disabled. Set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET in Railway environment variables.")
        return

    log.info("[XBOT] Starting — will post items with score ≥ 80%")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                current_items = get_items_fn()

                # Check news items
                await process_items(session, current_items)

                # Check price alerts
                await check_prices(session)

            except Exception as e:
                log.warning(f"[XBOT] Loop error: {e}")

            await asyncio.sleep(120)  # Check every 2 minutes

