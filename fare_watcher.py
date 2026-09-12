#!/usr/bin/env python3
"""
fare_watcher.py — Mistake / error fare watcher.

Polls free flight-deal feeds every run, keeps only fares that match YOUR
departure airports (and look like genuine error/mistake fares), ranks the
survivors with a local Ollama model, de-duplicates against past runs, and
pushes a Telegram alert (best deal first) for anything new.

Designed to be run on a schedule (Windows Task Scheduler, every ~5 min).
Stateless between runs except for seen.json (the de-dupe cache).
"""

from __future__ import annotations

import json
import sys
import time
import html
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
import feedparser

# ---------------------------------------------------------------------------
# CONFIG — edit these
# ---------------------------------------------------------------------------

# Telegram bot alerts (free). See README "Set up Telegram" for how to get these:
#   1. Message @BotFather -> /newbot -> copy the bot TOKEN.
#   2. Message your new bot once (say "hi"), then run: python fare_watcher.py --chatid
#      to auto-print your chat id. Paste it below.
TELEGRAM_BOT_TOKEN = "CHANGE-ME:paste-token-from-BotFather"   # overridden by secrets_local.py
TELEGRAM_CHAT_ID = "CHANGE-ME-chat-id"                        # overridden by secrets_local.py

# Your departure airports / cities. Case-insensitive substring match against the
# deal's title + summary. Include IATA codes AND city names for good coverage.
# Configured for major US hubs -> international destinations. Trim this list if
# you only care about certain regions. Empty list ([]) = alert on every fare.
ORIGIN_KEYWORDS = [
    # Northeast
    "JFK", "LGA", "EWR", "New York", "Newark",
    "BOS", "Boston", "PHL", "Philadelphia", "IAD", "DCA", "BWI", "Washington", "Baltimore",
    # Southeast
    "ATL", "Atlanta", "MIA", "FLL", "Miami", "Fort Lauderdale", "MCO", "Orlando", "CLT", "Charlotte",
    # Midwest
    "ORD", "MDW", "Chicago", "DTW", "Detroit", "MSP", "Minneapolis",
    # South-central
    "DFW", "Dallas", "IAH", "HOU", "Houston", "AUS", "Austin",
    # Mountain / West
    "DEN", "Denver", "PHX", "Phoenix", "LAS", "Las Vegas", "SEA", "Seattle",
    "SFO", "SJC", "OAK", "San Francisco", "LAX", "Los Angeles", "SAN", "San Diego",
]

# Optional: require the deal to look INTERNATIONAL. A post must contain one of
# these to alert. Set to [] to disable (also catch domestic fares).
DEST_KEYWORDS = [
    "Europe", "Asia", "Africa", "South America", "Caribbean", "Oceania", "Middle East",
    "London", "Paris", "Rome", "Madrid", "Barcelona", "Amsterdam", "Lisbon", "Athens",
    "Tokyo", "Bangkok", "Bali", "Seoul", "Singapore", "Dubai", "Istanbul", "Delhi",
    "Sydney", "Auckland", "Cancun", "Mexico City", "Rio", "Buenos Aires", "Cape Town",
    "Reykjavik", "Dublin", "Frankfurt", "Zurich", "Vienna", "Prague", "Cairo",
    "international", "roundtrip to", "RT to",
]

# Words that mark a post as an actual error/mistake fare. WIDE-NET MODE: this is
# now EMPTY, so posts don't need error-fare wording to qualify — any US-hub ->
# international deal passes the keyword stage and the local AI decides if it's
# worth alerting (see OLLAMA_MIN_SCORE). This catches strong deals + errors even
# when a blogger describes them in unusual words. To go back to errors-only,
# restore the sample list below.
MISTAKE_KEYWORDS = []  # was: ["error fare","mistake fare","glitch","pricing error","fare error","wow","insane"]

# Free RSS sources. Add or remove freely — any site with an RSS feed works.
# (Reddit's JSON API now blocks scripts, so we use its RSS feed instead.)
# All URLs below were verified live (returning entries) on 2026-09-11.
RSS_FEEDS = [
    # Dedicated error/mistake-fare trackers (highest signal)
    ("Secret Flying",        "https://www.secretflying.com/feed/"),
    ("The Flight Deal",      "https://www.theflightdeal.com/feed/"),
    ("Fly4Free",             "https://www.fly4free.com/feed/"),
    ("Airfarespot",          "https://www.airfarespot.com/feed/"),
    ("Thrifty Traveler",     "https://thriftytraveler.com/feed/"),
    # Community
    ("r/flightdeals",        "https://www.reddit.com/r/flightdeals/new/.rss"),
    ("r/TravelHacks",        "https://www.reddit.com/r/travelhacks/new/.rss"),
    # Points/travel blogs (occasional error fares; noise filtered by keywords+LLM)
    ("God Save the Points",  "https://godsavethepoints.com/feed/"),
    ("View from the Wing",   "https://viewfromthewing.com/feed/"),
    ("One Mile at a Time",   "https://onemileatatime.com/feed/"),
    ("The Points Guy",       "https://thepointsguy.com/feed/"),
    ("Frugal Flyer",         "https://frugalflyer.ca/feed/"),
    ("Point Me To The Plane","https://pointmetotheplane.boardingarea.com/feed/"),
]

# Reddit and some feeds reject generic/scripted User-Agents, so present a
# browser-style one.
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) fare-watcher/1.0"
}

# Keep the de-dupe cache next to this script.
BASE_DIR = Path(__file__).resolve().parent
SEEN_FILE = BASE_DIR / "seen.json"
LOG_FILE = BASE_DIR / "fare_watcher.log"
PRICE_STAMP = BASE_DIR / "price_scan.json"  # last time the price scan ran
MAX_SEEN = 800  # trim cache to this many most-recent IDs

# --- Ollama ranking (local LLM) --------------------------------------------
# Each candidate that passes the keyword filters is scored 0-100 by a local
# model: how likely it's a genuine mistake fare AND how exceptional the deal is.
# Alerts below OLLAMA_MIN_SCORE are dropped; the rest are sent best-first with
# the score + one-line reason attached. If Ollama is unreachable, the agent
# degrades gracefully: it sends the alert unscored rather than losing it.
USE_OLLAMA = True
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "phi3"          # installed locally; fast + good enough for scoring
OLLAMA_MIN_SCORE = 40          # WIDE-NET: 40 lets strong deals through, not just glitches (raise to be pickier)
OLLAMA_TIMEOUT = 120           # seconds per scoring call (first call loads model)
OLLAMA_MAX_SCORED = 15         # cap items scored per run so a burst can't stall it

# --- Below-value price scan (independent of the feeds) ---------------------
# Queries a flight-price API for each route on the watchlist and flags fares
# priced well below the route's typical price. Pick ONE provider (USE_* flag).
#
# QUOTA MATH: each route x date = 1 API search. SerpApi free = 250 searches/mo
# (~8/day). So the scan runs on its OWN daily cadence (NOT every 5 min like the
# feeds), and is budgeted to stay under quota:
#   len(PRICE_ROUTES) x len(PRICE_DATE_OFFSETS), capped by PRICE_MAX_CALLS,
#   run once per PRICE_SCAN_INTERVAL_HOURS.
# Default: 8 routes x 1 date = 8 searches/day ~= 240/mo. Raise your plan to
# widen the watchlist or add more dates.
PRICE_BELOW_PCT = 0.40         # flag fares >= this fraction below typical (0.40 = 40%)
PRICE_DATE_OFFSETS = [60]      # sample departures this many days out (add more = more searches)
PRICE_TRIP_DAYS = 7            # return this many days after departure (round trips)
PRICE_TRIP_TYPES = ["round", "oneway"]  # scan round-trip AND one-way international fares
PRICE_MAX_CALLS = 8            # hard cap on searches per scan (protects quota)
PRICE_SCAN_INTERVAL_HOURS = 24 # run the price scan at most this often
# Each search = 1 route x 1 date x 1 trip type. With both trip types the watchlist
# is bigger than the daily cap, so routes are ROTATED each day (see main): ~4
# routes x 2 types = 8/day, and the full list is covered every couple of days.

# Route watchlist: (origin IATA, destination IATA). Curated US hubs -> popular
# international destinations. 8 active routes = 8 searches/day on the free plan.
# Uncomment the extras (or add your own) if you upgrade your SerpApi plan.
PRICE_ROUTES = [
    ("JFK", "FCO"),  # New York -> Rome
    ("JFK", "LHR"),  # New York -> London
    ("JFK", "CDG"),  # New York -> Paris
    ("JFK", "NRT"),  # New York -> Tokyo
    ("EWR", "LIS"),  # Newark -> Lisbon
    ("BOS", "DUB"),  # Boston -> Dublin
    ("MIA", "BOG"),  # Miami -> Bogota
    ("ORD", "BCN"),  # Chicago -> Barcelona
    # ("LAX", "SYD"),  # Los Angeles -> Sydney   (uncomment on a paid plan)
    # ("SFO", "SIN"),  # San Francisco -> Singapore
]

# Price provider: SerpApi Google Flights (instant key, no approval).
# Get a key at https://serpapi.com/ and put it in secrets_local.py, then set USE_SERPAPI=True.
USE_SERPAPI = True
SERPAPI_KEY = "CHANGE-ME-serpapi-key"                        # overridden by secrets_local.py

# Load real credentials from an untracked local file (keeps secrets out of git).
# Create secrets_local.py with TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / SERPAPI_KEY.
try:
    import secrets_local as _secrets
    TELEGRAM_BOT_TOKEN = getattr(_secrets, "TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    TELEGRAM_CHAT_ID = getattr(_secrets, "TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)
    SERPAPI_KEY = getattr(_secrets, "SERPAPI_KEY", SERPAPI_KEY)
except ImportError:
    pass  # no local secrets file — placeholders stay (edit them or add secrets_local.py)

OLLAMA_PROMPT = (
    "You are a flight deal analyst. Judge whether a post is a genuine airline "
    "MISTAKE/ERROR fare (a pricing glitch far below normal), and how good the "
    "deal is.\n\n"
    "Return ONLY compact JSON: "
    '{{"score": <0-100 int>, "verdict": "<error_fare|good_deal|normal|unclear>", '
    '"reason": "<max 12 words>"}}\n\n'
    "score = likelihood it's a real error fare AND how exceptional "
    "(100 = obvious deep mistake fare, 0 = ordinary/not a deal).\n\n"
    "POST TITLE: {title}\nPOST TEXT: {text}\nJSON:"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')}  {msg}"
    print(line)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_seen() -> list[str]:
    if SEEN_FILE.exists():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _price_scan_due() -> bool:
    """True if at least PRICE_SCAN_INTERVAL_HOURS have passed since the last
    price scan (protects the API quota from the 5-minute feed cadence)."""
    try:
        last = json.loads(PRICE_STAMP.read_text(encoding="utf-8")).get("last", 0)
    except (OSError, json.JSONDecodeError):
        last = 0
    return (time.time() - float(last)) >= PRICE_SCAN_INTERVAL_HOURS * 3600


def _mark_price_scan() -> None:
    try:
        PRICE_STAMP.write_text(json.dumps({"last": time.time()}), encoding="utf-8")
    except OSError as e:
        log(f"WARN could not write price-scan stamp: {e}")


def save_seen(seen: list[str]) -> None:
    trimmed = seen[-MAX_SEEN:]
    try:
        SEEN_FILE.write_text(json.dumps(trimmed), encoding="utf-8")
    except OSError as e:
        log(f"WARN could not write seen cache: {e}")


def clean(text: str) -> str:
    """Strip HTML tags/entities from a summary blob."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def matches_filters(title: str, summary: str) -> bool:
    blob = f"{title} {summary}".lower()
    origin_ok = (not ORIGIN_KEYWORDS) or any(k.lower() in blob for k in ORIGIN_KEYWORDS)
    mistake_ok = (not MISTAKE_KEYWORDS) or any(k.lower() in blob for k in MISTAKE_KEYWORDS)
    dest_ok = (not DEST_KEYWORDS) or any(k.lower() in blob for k in DEST_KEYWORDS)
    return origin_ok and mistake_ok and dest_ok


# ---------------------------------------------------------------------------
# Source collectors — each returns list of {id,title,url,summary,source}
# Each is wrapped so one failing source never kills the whole run.
# ---------------------------------------------------------------------------

def collect_rss(name: str, url: str) -> list[dict]:
    out = []
    try:
        parsed = feedparser.parse(url, request_headers=HTTP_HEADERS)
        for e in parsed.entries:
            link = getattr(e, "link", "")
            out.append({
                "id": getattr(e, "id", link) or link,
                "title": clean(getattr(e, "title", "")),
                "url": link,
                "summary": clean(getattr(e, "summary", "")),
                "source": name,
            })
    except Exception as e:  # noqa: BLE001 - feed parsing can raise many things
        log(f"WARN {name} failed: {e}")
    return out


def collect_all() -> list[dict]:
    items: list[dict] = []
    for name, url in RSS_FEEDS:
        items.extend(collect_rss(name, url))
    return items


# ---------------------------------------------------------------------------
# Ollama ranking
# ---------------------------------------------------------------------------

def score_fare(item: dict) -> dict | None:
    """Score a candidate via the local model. Returns {score,verdict,reason}
    or None if scoring failed (caller should then send the alert unscored)."""
    prompt = OLLAMA_PROMPT.format(title=item["title"], text=item["summary"][:500])
    try:
        r = requests.post(
            OLLAMA_URL,
            json={
                "model": OLLAMA_MODEL,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {"temperature": 0.1, "num_predict": 80},
            },
            timeout=OLLAMA_TIMEOUT,
        )
        r.raise_for_status()
        data = json.loads(r.json()["response"])
        score = int(data.get("score", 0))
        return {
            "score": max(0, min(100, score)),
            "verdict": str(data.get("verdict", "unclear")),
            "reason": str(data.get("reason", "")).strip(),
        }
    except Exception as e:  # noqa: BLE001 - network/JSON/parse errors
        log(f"WARN ollama scoring failed for {item['url']}: {e}")
        return None


def rank_candidates(candidates: list[dict]) -> list[dict]:
    """Attach an Ollama score to each candidate, drop those below the
    threshold, and return them sorted best-first. Unscored items (Ollama down)
    are kept and treated as top priority so real alerts are never lost."""
    if not USE_OLLAMA or not candidates:
        return candidates

    scored = 0
    for item in candidates:
        if scored >= OLLAMA_MAX_SCORED:
            item["_rank"] = -1  # not scored this run; keep, sort after scored ones
            continue
        result = score_fare(item)
        scored += 1
        if result is None:
            item["_rank"] = 999  # Ollama failed -> don't lose it, send first
            continue
        item["_ollama"] = result
        item["_rank"] = result["score"]

    kept = [
        it for it in candidates
        if it.get("_rank", 999) >= OLLAMA_MIN_SCORE or "_ollama" not in it
    ]
    dropped = len(candidates) - len(kept)
    if dropped:
        log(f"Ollama dropped {dropped} low-score candidate(s) (< {OLLAMA_MIN_SCORE}).")
    kept.sort(key=lambda it: it.get("_rank", 0), reverse=True)
    return kept


# ---------------------------------------------------------------------------
# Alerting via Telegram Bot API
# ---------------------------------------------------------------------------

def _tg_api(method: str) -> str:
    return f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"


def send_push(item: dict) -> bool:
    if "CHANGE-ME" in TELEGRAM_BOT_TOKEN or "CHANGE-ME" in TELEGRAM_CHAT_ID:
        log("ERROR TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID still placeholder — edit fare_watcher.py.")
        return False
    # HTML message: score badge + bold source + linked title + summary.
    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    header = f"✈️ <b>{esc(item['source'])}</b>"
    rank = item.get("_ollama")
    if rank:
        # Traffic-light badge by score band.
        dot = "🟢" if rank["score"] >= 80 else ("🟡" if rank["score"] >= 65 else "🟠")
        header = (
            f"{dot} <b>{rank['score']}/100</b> · {esc(rank['verdict'])}"
            f"  ·  {esc(item['source'])}"
        )
    text = f"{header}\n<a href=\"{esc(item['url'])}\">{esc(item['title'])}</a>"
    if rank and rank["reason"]:
        text += f"\n<i>{esc(rank['reason'])}</i>"
    if item["summary"]:
        text += f"\n\n{esc(item['summary'][:500])}"
    try:
        resp = requests.post(
            _tg_api("sendMessage"),
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"WARN push failed for {item['url']}: {e}")
        return False


def print_chat_id() -> int:
    """Helper: run with --chatid after messaging your bot to discover chat id."""
    if "CHANGE-ME" in TELEGRAM_BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN in fare_watcher.py first.")
        return 1
    try:
        r = requests.get(_tg_api("getUpdates"), timeout=20)
        r.raise_for_status()
        results = r.json().get("result", [])
        if not results:
            print("No messages found. Send your bot a message first, then re-run --chatid.")
            return 1
        ids = {u["message"]["chat"]["id"]: u["message"]["chat"].get("username", "")
               for u in results if "message" in u}
        print("Chat id(s) that have messaged your bot:")
        for cid, uname in ids.items():
            print(f"  {cid}   (@{uname})" if uname else f"  {cid}")
        print("\nPaste the id into TELEGRAM_CHAT_ID in fare_watcher.py.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"Error calling getUpdates: {e}")
        return 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    seen = load_seen()
    seen_set = set(seen)

    items = collect_all()
    log(f"Fetched {len(items)} items from {len(RSS_FEEDS)} sources.")

    # Phase 1: find NEW items that pass the keyword filters. Mark every new item
    # as seen (even non-matches) so we never re-check it on later runs.
    candidates = []
    for item in items:
        uid = item["id"]
        if not uid or uid in seen_set:
            continue
        seen_set.add(uid)
        seen.append(uid)
        if matches_filters(item["title"], item["summary"]):
            candidates.append(item)

    # Phase 1b: independent below-value price scan. Runs on its OWN daily
    # cadence (not every feed poll) to protect the API quota. Hits are already
    # route-qualified, so they skip the keyword filter but still get de-duped,
    # ranked, and alerted like everything else. Never let it break a run.
    price_hits = []
    if USE_SERPAPI and _price_scan_due():
        _mark_price_scan()  # stamp first so a crash mid-scan can't re-spend quota
        # Rotate the route list by day so both trip types fit the daily quota
        # while every route still gets covered within a couple of days.
        day = int(time.time() // 86400)
        shift = (day * PRICE_MAX_CALLS) % max(len(PRICE_ROUTES), 1)
        rotated_routes = PRICE_ROUTES[shift:] + PRICE_ROUTES[:shift]
        try:
            import serpapi_prices
            price_hits = serpapi_prices.scan_below_value(
                SERPAPI_KEY, rotated_routes, PRICE_DATE_OFFSETS, PRICE_BELOW_PCT,
                trip_days=PRICE_TRIP_DAYS, max_calls=PRICE_MAX_CALLS,
                trip_types=PRICE_TRIP_TYPES, log=log,
            )
        except Exception as e:  # noqa: BLE001
            log(f"WARN price scan errored: {e}")

    for hit in price_hits:
        if hit["id"] in seen_set:
            continue
        seen_set.add(hit["id"])
        seen.append(hit["id"])
        candidates.append(hit)

    # Phase 2: rank with the local model (drops weak matches, sorts best-first).
    ranked = rank_candidates(candidates)

    # Phase 3: alert, best deal first.
    new_alerts = 0
    for item in ranked:
        if send_push(item):
            new_alerts += 1
            badge = f"{item['_ollama']['score']}/100 " if item.get("_ollama") else ""
            log(f"ALERT {badge}[{item['source']}] {item['title']}")
            time.sleep(1)  # be gentle with the Telegram API

    save_seen(seen)
    log(f"Done. {len(candidates)} candidate(s), {new_alerts} alert(s) sent.")
    return 0


if __name__ == "__main__":
    if "--chatid" in sys.argv:
        sys.exit(print_chat_id())
    sys.exit(main())
