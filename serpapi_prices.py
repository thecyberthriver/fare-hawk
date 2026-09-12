"""
serpapi_prices.py — independent below-value fare detection via SerpApi's
Google Flights engine.

For each route on a watchlist it pulls the current cheapest round-trip price AND
Google's own "typical price range" for those dates, then flags anything priced
well below that typical range — catching mistake fares before they hit any feed.

Why SerpApi: instant API key (no business approval), real Google Flights data,
and `price_insights.typical_price_range` gives a ready-made baseline so we don't
have to compute our own median. Free trial ~100 searches/mo; get a key at
https://serpapi.com/ and paste it into fare_watcher.py.

Exposes scan_below_value(...) -> list[candidate dict] in the same shape
fare_watcher.py uses, so hits flow through the same Ollama ranking + Telegram
alerting + de-dupe pipeline.
"""

from __future__ import annotations

from datetime import date, timedelta

import requests

SEARCH_URL = "https://serpapi.com/search.json"


def _search(api_key, origin, dest, dep, ret, currency, trip_type="round"):
    params = {
        "engine": "google_flights",
        "departure_id": origin,
        "arrival_id": dest,
        "outbound_date": dep,
        "currency": currency,
        "hl": "en",
        "api_key": api_key,
        "type": "2" if trip_type == "oneway" else "1",  # 1=round trip, 2=one way
    }
    if trip_type != "oneway":
        params["return_date"] = ret  # one-way must NOT send a return date
    r = requests.get(SEARCH_URL, params=params, timeout=45)
    r.raise_for_status()
    return r.json()


def _current_price(payload):
    """Cheapest round-trip price from the response, or None."""
    pi = payload.get("price_insights") or {}
    if pi.get("lowest_price") is not None:
        return float(pi["lowest_price"])
    flights = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    prices = [f["price"] for f in flights if f.get("price") is not None]
    return float(min(prices)) if prices else None


def scan_below_value(
    api_key, routes, date_offsets, below_pct,
    currency="USD", trip_days=7, max_calls=30, trip_types=("round",), log=print,
):
    """Scan the route watchlist and return below-value candidate dicts.

    Checks each requested trip type ("round" and/or "oneway"). A route/date/type
    is flagged when the current cheapest price is at or below typical_midpoint *
    (1 - below_pct), where typical_midpoint is the middle of Google's
    typical_price_range. Returns [] on failure (never raises).
    """
    out = []
    if "CHANGE-ME" in str(api_key):
        log("ERROR SerpApi key still placeholder — edit fare_watcher.py.")
        return out

    today = date.today()
    calls = 0
    for origin, dest in routes:
        for trip_type in trip_types:
            oneway = trip_type == "oneway"
            label = "OW" if oneway else "RT"
            for off in date_offsets:
                if calls >= max_calls:
                    log(f"SerpApi hit max_calls={max_calls}; stopping scan.")
                    return out
                dep = (today + timedelta(days=off)).isoformat()
                ret = (today + timedelta(days=off + trip_days)).isoformat()
                calls += 1
                try:
                    payload = _search(api_key, origin, dest, dep, ret, currency, trip_type)
                except Exception as e:  # noqa: BLE001
                    log(f"WARN serpapi {origin}-{dest} {label} {dep}: {e}")
                    continue
                if payload.get("error"):
                    log(f"WARN serpapi {origin}-{dest} {label} {dep}: {payload['error']}")
                    continue

                pi = payload.get("price_insights") or {}
                trange = pi.get("typical_price_range") or []
                current = _current_price(payload)
                if current is None or len(trange) < 2 or not trange[0] or not trange[1]:
                    continue
                low, high = float(trange[0]), float(trange[1])
                typical = (low + high) / 2
                # Flag only when BELOW the floor of Google's typical range AND at
                # least below_pct under the midpoint. The floor check kills false
                # positives on routes with a very wide typical range.
                threshold = min(low, typical * (1 - below_pct))
                if current <= threshold:
                    pct = round((1 - current / typical) * 100)
                    level = pi.get("price_level", "")
                    when = (f"Depart {dep}, return {ret}." if not oneway
                            else f"One-way, depart {dep}.")
                    # Prefer the exact Google Flights URL SerpApi returns — it
                    # encodes the trip type (one-way vs round-trip) and dates, so
                    # the link opens correctly instead of defaulting to round-trip.
                    sm = payload.get("search_metadata") or {}
                    gf_url = sm.get("google_flights_url")
                    if not gf_url:
                        q = (f"flights%20{origin}%20to%20{dest}%20{dep}"
                             + ("" if oneway else f"%20{ret}"))
                        gf_url = f"https://www.google.com/travel/flights?q={q}"
                    out.append({
                        "id": f"serpapi:{origin}-{dest}:{dep}:{label}:{int(current)}",
                        "source": "Google Flights price scan",
                        "title": f"Below-value: {origin}->{dest} ${int(current)} {label} "
                                 f"({pct}% under typical)",
                        "url": gf_url,
                        "summary": f"Current cheapest ${int(current)} ({label}) vs typical "
                                   f"${int(trange[0])}-${int(trange[1])} "
                                   f"(Google rates it '{level}'). {when} {pct}% below typical.",
                    })
                    log(f"SerpApi BELOW-VALUE {origin}-{dest} {label} {dep}: "
                        f"${int(current)} vs typical ~${int(typical)} ({pct}%)")
    log(f"SerpApi scan done: {calls} searches, {len(out)} below-value hit(s).")
    return out
