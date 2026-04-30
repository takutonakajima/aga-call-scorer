#!/usr/bin/env python3
"""
AGA Daily Dial Tracker — runs every 30 min via launchd.

Counts every Twilio recording (dial) per rep per day so the dashboard can show
"Calls Today: 142 / 200" and Sophia can see who's hit quota at a glance.

How it works:
  1. Fetches today's recordings from Twilio (since midnight EST).
  2. Identifies the rep for each recording by looking up the parent call.
  3. Caches recording_sid → rep so repeat runs are fast.
  4. Tallies dials and connects (calls ≥30s) per rep per day.
  5. POSTs each (rep, date) tally to the Daily Dials Ingest webhook.
"""
import json
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import defaultdict
from pathlib import Path

# Add scripts dir to path then import shared config
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import (TWILIO_ACCOUNT, TWILIO_AUTH, GEMINI_KEY, REP_MAP,
                     SCORE_INGEST, SCORE_API, DIALS_INGEST, TIPS_INGEST,
                     TOPCALL_INGEST, ALERT_WEBHOOK)






EST = ZoneInfo("America/New_York")  # auto-handles EST/EDT
MIN_CONVERSATION = 30  # seconds — defines a connect

CACHE_FILE = Path.home() / ".aga-recording-rep-cache.json"
LOG_DIR = Path.home() / "Library/Logs/aga-call-scorer"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def fetch(url):
    _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
    return json.loads(body)


def load_cache():
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_cache(cache):
    # Keep only the last 5000 entries to avoid unbounded growth
    if len(cache) > 5000:
        cache = dict(list(cache.items())[-5000:])
    CACHE_FILE.write_text(json.dumps(cache))


def list_today_recordings():
    """All recordings since midnight EST today."""
    midnight_est = datetime.now(EST).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_utc = midnight_est.astimezone(timezone.utc)
    since = midnight_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}"
        f"/Recordings.json?PageSize=200"
        f"&DateCreated%3E={urllib.parse.quote(since)}"
    )
    all_recs = []
    page = 0
    while url and page < 20:
        page += 1
        data = fetch(url)
        all_recs.extend(data.get("recordings", []))
        nxt = data.get("next_page_uri")
        url = f"https://api.twilio.com{nxt}" if nxt else None
    return all_recs


def get_call(sid):
    if not sid:
        return None
    try:
        return fetch(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}/Calls/{sid}.json"
        )
    except urllib.error.HTTPError:
        return None


def identify_rep(recording, cache):
    sid = recording.get("sid")
    if sid in cache:
        return cache[sid]
    call = get_call(recording.get("call_sid"))
    parent = get_call(call.get("parent_call_sid")) if call else None
    from_field = ""
    if parent:
        from_field = (parent.get("from") or "").replace("client:", "")
    if not from_field and call:
        from_field = (call.get("from") or "").replace("client:", "")
    rep = REP_MAP.get(from_field[:7])
    cache[sid] = rep  # may be None
    return rep


def post_count(rep, date, dials, connects):
    payload = {
        "rep": rep,
        "date": date,
        "dials": dials,
        "connects": connects,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    status, _ = http(
        "POST", DIALS_INGEST,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    return status


def main():
    today = datetime.now(EST).strftime("%Y-%m-%d")
    log(f"=== Dial tracker — {today} (EST) ===")

    cache = load_cache()
    recs = list_today_recordings()
    log(f"Today's recordings: {len(recs)} (cache: {len(cache)} entries)")

    tally = defaultdict(lambda: {"dials": 0, "connects": 0})
    for r in recs:
        rep = identify_rep(r, cache)
        if not rep:
            continue
        dur = int(r.get("duration", "0") or 0)
        tally[rep]["dials"] += 1
        if dur >= MIN_CONVERSATION:
            tally[rep]["connects"] += 1

    save_cache(cache)

    # Always post zeros for known reps if no activity yet (so dashboard renders all reps)
    for rep in REP_MAP.values():
        stats = tally.get(rep, {"dials": 0, "connects": 0})
        try:
            post_count(rep, today, stats["dials"], stats["connects"])
            log(f"  {rep}: {stats['dials']} dials / {stats['connects']} connects → ingested")
        except Exception as e:
            log(f"  {rep}: FAIL {type(e).__name__}: {e}")

    log("=== Done ===")


if __name__ == "__main__":
    main()
