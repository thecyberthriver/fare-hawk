# Fare Watcher — mistake/error fare alerts to your phone

A small local agent that polls free flight-deal feeds every 15 minutes, keeps
only fares that match your departure airports and look like genuine error
fares, de-duplicates against past runs, and pushes a **phone alert** for
anything new — no paid subscription, no signup.

```
Task Scheduler (every 15 min)
        │
        ▼
  fare_watcher.py ── polls ─► Secret Flying RSS
                    ├─ polls ─► The Flight Deal RSS
                    └─ polls ─► Reddit r/flightdeals JSON
        │
   filter by YOUR airports + "error fare" wording
        │
   de-dupe against seen.json
        │
        ▼
   Telegram Bot API ──push──►  📱 Telegram on your phone
```

## Setup (about 5 minutes)

### 1. Install Python deps
```powershell
cd C:\Users\gokon\fare-watcher
pip install -r requirements.txt
```

### 2. Set up Telegram (free)
1. In Telegram, message **@BotFather** → send `/newbot` → follow prompts →
   copy the **bot token** it gives you (looks like `123456:ABC-DEF...`).
2. Open `fare_watcher.py` and paste it into `TELEGRAM_BOT_TOKEN`.
3. Find your new bot in Telegram and send it any message (e.g. "hi") — this is
   required before the bot can message you back.
4. Discover your chat id:
   ```powershell
   python fare_watcher.py --chatid
   ```
   Paste the printed number into `TELEGRAM_CHAT_ID`.
   > Tip: to alert a **group**, add the bot to the group, post a message there,
   > then run `--chatid` — the group id (a negative number) will appear.

### 3. Set your airports / destinations
Already configured for **major US hubs → international destinations**. Edit
`ORIGIN_KEYWORDS` to narrow to specific cities, and `DEST_KEYWORDS` to control
the international filter (set `DEST_KEYWORDS = []` to also catch domestic fares).

### 4. Test it once
```powershell
python fare_watcher.py
```
You should see log output and (if any new matching fares exist) a push on your
phone. To force a test push, temporarily set `ORIGIN_KEYWORDS = []` and
`MISTAKE_KEYWORDS = []`, run once, then revert.

### 5. Schedule it every 15 minutes
Open an **Admin** PowerShell and run:
```powershell
cd C:\Users\gokon\fare-watcher
.\register_task.ps1
```
Manage/disable it later in **Task Scheduler → FareWatcher**.

## Files
| File | Purpose |
|---|---|
| `fare_watcher.py` | The agent. All config is at the top. Polls 13 verified feeds. |
| `serpapi_prices.py` | Below-value scanner via SerpApi Google Flights. |
| `requirements.txt` | `requests`, `feedparser`. |
| `register_task.ps1` | Registers the 5-min silent scheduled task. |
| `seen.json` | Auto-created de-dupe cache (safe to delete to reset). |
| `price_scan.json` | Auto-created timestamp of the last price scan (quota gate). |
| `fare_watcher.log` | Append-only run log. |

## Tuning
- **Too noisy?** Add more specific `ORIGIN_KEYWORDS`, or tighten `MISTAKE_KEYWORDS`.
- **Missing deals?** Loosen/empty `MISTAKE_KEYWORDS`, add more feeds to `RSS_FEEDS`.
- **More sources:** any site with an RSS feed drops straight into `RSS_FEEDS`.

## Ollama ranking (built in)
Every candidate that passes the keyword filters is scored **0–100** by a local
model (`phi3`) that judges how likely it's a genuine mistake fare and how
exceptional the deal is. Alerts below the threshold are dropped, and the rest
arrive **best-first** with a badge like `🟢 90/100 · error_fare` and a one-line
reason. If Ollama isn't running, the agent degrades gracefully — it sends the
alert unscored rather than losing it.

Config at the top of `fare_watcher.py`:
| Setting | Meaning |
|---|---|
| `USE_OLLAMA` | Master on/off switch. |
| `OLLAMA_MODEL` | Local model to use (`phi3` default; `ollama list` to see yours). |
| `OLLAMA_MIN_SCORE` | Drop alerts scoring below this (default 55). Raise to be pickier. |
| `OLLAMA_MAX_SCORED` | Cap items scored per run so a burst can't stall it. |

> Requires the Ollama server running (`ollama serve`, or the desktop app) and
> the model pulled (`ollama pull phi3`).

## Below-value detection (built, off until a key is added)
Independently queries a flight-price API for each route on `PRICE_ROUTES` and
flags fares priced **≥40% below typical** (`PRICE_BELOW_PCT`) — catching mistake
fares before they hit any feed. Hits flow through the same Ollama ranking +
Telegram alerting as feed items.

**Provider — SerpApi Google Flights** (instant key, no business approval, real
prices + Google's "typical price range" as the baseline):
1. Get a key at **serpapi.com** (free plan = 250 searches/mo).
2. In `fare_watcher.py`, paste it into `SERPAPI_KEY` and set `USE_SERPAPI = True`.
3. Tune `PRICE_ROUTES` and `PRICE_BELOW_PCT` to taste.

**Quota / cadence:** each route×date×trip-type = 1 search. To stay under the free
250/mo, the price scan runs on its **own daily cadence** (`PRICE_SCAN_INTERVAL_HOURS`,
tracked in `price_scan.json`) — NOT every 5-minute feed poll. Both round-trip and
one-way are scanned; routes rotate daily so ~8 searches/day ≈ 240/mo covers the
list every couple of days. Widen `PRICE_ROUTES` only if you raise your plan.

> Amadeus was evaluated but its free self-service tier was **decommissioned
> Jul 17, 2026** (enterprise contract only now). GDS systems (Sabre, Travelport)
> likewise need a travel business account. The `scan_below_value()` signature is
> provider-agnostic, so another collector (e.g. Duffel) could drop in later.

## Possible upgrades (not built yet)
- **ntfy.sh** as an alternative alert channel if you ever want signup-free push.
