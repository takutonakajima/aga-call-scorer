#!/usr/bin/env python3
"""
AGA Pipeline Health Monitor — runs hourly via GitHub Actions.

Checks every critical data pipeline for staleness and DMs Juan + Sophia if
anything's broken. The goal: never have another 26-hour silent outage like
Scenario 04 had on Apr 30.

Checks:
  • Booking pipeline (Scenario 04 → data store 95606): latest booking < 12h old?
  • Dial tracker (GitHub Actions every 30 min → data store 96058): updated < 90m?
  • Call scorer (GitHub Actions every 15 min → data store 95103): newest score < 4h old?
  • Weekly tips (Mondays → data store 96036): record exists for this week?
  • Top call (Mondays → data store 96069): record exists for this week?

Anti-spam: each broken pipeline only alerts once per 24h via the anomaly state
data store.
"""
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import (REP_MAP, ALERT_WEBHOOK,
                     SCORE_API, ANOMALY_STATE_API, ANOMALY_STATE_INGEST)
from _schedule import EST

# Live read APIs (the dashboards already use these)
BOOKING_API = "https://hook.us2.make.com/zta4icvee2vs637c7h823dbbvl3dma3g"
DIALS_API   = "https://hook.us2.make.com/iqiuk23ks9uscx79rs3b5pqj85tnugqi"
TIPS_API    = "https://hook.us2.make.com/92wixa36ff24n1k6dt57a4mn4qrfxylb"
TOPCALL_API = "https://hook.us2.make.com/2ncatyrspbm62lt98vah5ef362sf89wv"

# Staleness thresholds — minutes
BOOKING_STALE_MIN = 12 * 60       # 12h — bookings can be slow during off hours
DIAL_STALE_MIN    = 90            # tracker runs every 30m, alert if ≥3 missed
SCORE_STALE_MIN   = 4 * 60        # 4h gives buffer for slow Gemini days

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


def fetch_json(url):
    _, body = http("POST", url, headers={"Content-Type": "application/json"}, data=b"{}")
    return json.loads(body)


def parse_iso(s):
    """Parse ISO8601 / RFC3339 / GHL format → UTC datetime, or None on failure."""
    if not s: return None
    try:
        if s.endswith("Z"): s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        try:
            return parsedate_to_datetime(s).astimezone(timezone.utc)
        except Exception:
            return None


def age_minutes(dt):
    if not dt: return None
    return int((datetime.now(timezone.utc) - dt).total_seconds() / 60)


def fmt_age(mins):
    if mins is None: return "never"
    if mins < 60: return f"{mins}m ago"
    h, m = mins // 60, mins % 60
    return f"{h}h {m}m ago" if m else f"{h}h ago"


# ---- Anti-spam state (re-uses anomaly state store) -------------------------
def is_already_alerted_today(pipeline):
    """Check if we already fired this health alert today."""
    if not ANOMALY_STATE_API:
        return False
    try:
        arr = fetch_json(ANOMALY_STATE_API) or []
        today = datetime.now(EST).strftime("%Y-%m-%d")
        for r in arr:
            if r.get("rep") == "_HEALTH" and r.get("pattern") == pipeline:
                return r.get("alerted_date") == today
    except Exception:
        pass
    return False


def mark_alerted(pipeline):
    if not ANOMALY_STATE_INGEST:
        return
    try:
        http("POST", ANOMALY_STATE_INGEST,
             headers={"Content-Type": "application/json"},
             data=json.dumps({
                 "rep": "_HEALTH",
                 "pattern": pipeline,
                 "alerted_date": datetime.now(EST).strftime("%Y-%m-%d"),
             }).encode())
    except Exception as e:
        log(f"  warn: state write failed ({e})")


# ---- Alert sender ----------------------------------------------------------
def fire(pipeline, title, message):
    if is_already_alerted_today(pipeline):
        log(f"  {pipeline}: already alerted today, skipping DM")
        return
    payload = {
        "rep": "_SYSTEM",
        "pattern": f"HEALTH_{pipeline}",
        "title": title,
        "message": message,
        "stats": {"dials": 0, "connects": 0},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        http("POST", ALERT_WEBHOOK,
             headers={"Content-Type": "application/json"},
             data=json.dumps(payload).encode())
        mark_alerted(pipeline)
        log(f"  → ALERT fired: {pipeline}")
    except Exception as e:
        log(f"  → alert failed: {e}")


# ---- Individual checks ------------------------------------------------------
def check_bookings():
    name = "BOOKING_PIPELINE"
    try:
        data = fetch_json(BOOKING_API) or []
        if not data:
            fire(name, "🚨 Booking pipeline: zero records",
                 "Booking data store is empty. Scenario 04 may be broken or the data store was wiped.")
            return False
        latest = max((parse_iso(b.get("event_time")) for b in data if b.get("event_time")), default=None)
        mins = age_minutes(latest)
        log(f"  bookings: latest {fmt_age(mins)}")
        if mins is not None and mins > BOOKING_STALE_MIN:
            fire(name, f"🚨 Bookings stalled — last one {fmt_age(mins)}",
                 f"No new bookings have flowed in via Scenario 04 for over {BOOKING_STALE_MIN//60}h.\n"
                 f"Most likely: GHL webhook disconnected, Scenario 04 has errors, or the data store hit a quota.\n"
                 f"Check: https://us2.make.com/scenarios/4890311")
            return False
        return True
    except Exception as e:
        log(f"  bookings: check failed ({e})")
        return None


def check_dials():
    name = "DIAL_TRACKER"
    try:
        data = fetch_json(DIALS_API) or []
        if not data:
            fire(name, "🚨 Dial tracker: zero records",
                 "Daily dial counts data store is empty.")
            return False
        latest = max((parse_iso(d.get("updated_at")) for d in data if d.get("updated_at")), default=None)
        mins = age_minutes(latest)
        log(f"  dials: latest update {fmt_age(mins)}")
        if mins is not None and mins > DIAL_STALE_MIN:
            fire(name, f"⚠️ Dial tracker stalled — last update {fmt_age(mins)}",
                 f"GitHub Actions dial-tracker workflow hasn't updated counts in over {DIAL_STALE_MIN}m.\n"
                 f"Check: https://github.com/takutonakajima/aga-call-scorer/actions/workflows/dial-tracker.yml")
            return False
        return True
    except Exception as e:
        log(f"  dials: check failed ({e})")
        return None


def check_scores():
    name = "CALL_SCORER"
    try:
        data = fetch_json(SCORE_API) or []
        if not data:
            log("  scores: 0 records (might be valid for a fresh week)")
            return True
        latest = max((parse_iso(c.get("date")) for c in data if c.get("date")), default=None)
        mins = age_minutes(latest)
        log(f"  scores: latest call {fmt_age(mins)}")
        # Only alert during business hours when we'd expect activity
        now_est = datetime.now(EST)
        is_business = 6 <= now_est.hour < 23  # 6am – 11pm ET
        if is_business and mins is not None and mins > SCORE_STALE_MIN:
            fire(name, f"⚠️ Call scorer stalled — newest call {fmt_age(mins)}",
                 f"No new scored calls have arrived in {SCORE_STALE_MIN//60}h+. Either reps aren't dialing, "
                 f"or the GitHub Actions scoring workflow is broken.\n"
                 f"Check: https://github.com/takutonakajima/aga-call-scorer/actions/workflows/score-calls.yml")
            return False
        return True
    except Exception as e:
        log(f"  scores: check failed ({e})")
        return None


def check_weekly_tips():
    name = "WEEKLY_TIPS"
    try:
        # Only run this check on Tuesday+ — Mondays haven't run yet
        now_est = datetime.now(EST)
        if now_est.weekday() == 0 and now_est.hour < 14:
            return True
        data = fetch_json(TIPS_API) or []
        # Build current week's Monday EST
        monday = (now_est - timedelta(days=now_est.weekday())).strftime("%Y-%m-%d")
        present = [t for t in data if t.get("week") == monday]
        log(f"  weekly_tips: {len(present)} reps have tips for {monday}")
        if len(present) < 3:  # at least 3 of 4 reps
            fire(name, f"⚠️ Weekly tips missing for {monday}",
                 f"Only {len(present)} of 4 reps have weekly tips for this week. "
                 f"weekly_digest.py may have failed Monday morning.")
            return False
        return True
    except Exception as e:
        log(f"  weekly_tips: check failed ({e})")
        return None


def check_top_call():
    name = "TOP_CALL"
    try:
        now_est = datetime.now(EST)
        if now_est.weekday() == 0 and now_est.hour < 14:
            return True
        data = fetch_json(TOPCALL_API) or []
        monday = (now_est - timedelta(days=now_est.weekday())).strftime("%Y-%m-%d")
        present = [t for t in data if t.get("week") == monday]
        log(f"  top_call: {len(present)} record for {monday}")
        if not present:
            fire(name, f"⚠️ Top Call missing for {monday}",
                 f"top_call.py didn't produce a Top Call record this Monday. "
                 f"Mac may have been asleep or the script erroried.")
            return False
        return True
    except Exception as e:
        log(f"  top_call: check failed ({e})")
        return None


def main():
    log(f"=== Pipeline Health Monitor @ {datetime.now(EST).strftime('%a %Y-%m-%d %H:%M %Z')} ===")
    results = {
        "BOOKING_PIPELINE": check_bookings(),
        "DIAL_TRACKER":     check_dials(),
        "CALL_SCORER":      check_scores(),
        "WEEKLY_TIPS":      check_weekly_tips(),
        "TOP_CALL":         check_top_call(),
    }
    log("=== Summary ===")
    for k, v in results.items():
        emoji = "✅" if v is True else "❌" if v is False else "⏭️"
        log(f"  {emoji} {k}")
    log("Done.")


if __name__ == "__main__":
    main()
