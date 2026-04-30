#!/usr/bin/env python3
"""
AGA Anomaly Detector — runs hourly via launchd.

Scans recent Twilio activity per rep and flags suspicious patterns:

  1. VOICEMAIL_WALL
     20+ dials in last 4h with 0 conversations ≥30s. (Julie's pattern.)
     "Tons of dials, no real conversations" — connection issue or dialer problem.

  2. NO_CONVERSATIONS
     5+ dials in last 2h with 0 conversations ≥30s. Earlier-stage version
     of voicemail wall — caught faster.

  3. LONG_GAP
     Rep had real conversations earlier today but has been silent
     (zero dials) for 2+ hours.

When a pattern fires, posts a diagnostic message to the Slack alerts webhook
(Make.com scenario routes to Sophia + Juan). State file prevents re-alerting
the same pattern more than once per rep per day.
"""
import json
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from pathlib import Path

# Add scripts dir to path then import shared config
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import (TWILIO_ACCOUNT, TWILIO_AUTH, GEMINI_KEY, REP_MAP,
                     SCORE_INGEST, SCORE_API, DIALS_INGEST, TIPS_INGEST,
                     TOPCALL_INGEST, ALERT_WEBHOOK)

# ---- CONFIG -----------------------------------------------------------------





STATE_FILE = Path.home() / ".aga-anomaly-state"
LOG_DIR = Path.home() / "Library/Logs/aga-call-scorer"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Thresholds
VOICEMAIL_WALL_DIALS = 20          # 20+ dials, 0 connects in 4h window
VOICEMAIL_WALL_WINDOW = 4
LOW_CONNECT_RATE_DIALS = 30        # 30+ dials with connect rate < 5% — Julie's pattern
LOW_CONNECT_RATE_THRESHOLD = 0.05
NO_CONVERSATIONS_DIALS = 5         # 5+ dials, 0 connects in 2h
NO_CONVERSATIONS_WINDOW = 2
LONG_GAP_HOURS = 2
MIN_CONVERSATION_DUR = 30          # seconds — defines a "real conversation"


# ---- HELPERS ----------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http(method, url, headers=None, data=None, timeout=30):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def already_alerted_today(state, rep, pattern):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return state.get(rep, {}).get(pattern) == today


def mark_alerted(state, rep, pattern):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state.setdefault(rep, {})[pattern] = today


# ---- TWILIO -----------------------------------------------------------------
def fetch(url):
    _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
    return json.loads(body)


def list_recent_recordings(hours):
    """All recordings created in last N hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}"
        f"/Recordings.json?PageSize=200"
        f"&DateCreated%3E={urllib.parse.quote(since_str)}"
    )
    all_recs = []
    page = 0
    while url and page < 10:
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


def identify_rep(recording):
    """Look up parent call to find which rep dialed."""
    call = get_call(recording.get("call_sid"))
    parent = get_call(call.get("parent_call_sid")) if call else None
    from_field = ""
    if parent:
        from_field = (parent.get("from") or "").replace("client:", "")
    if not from_field and call:
        from_field = (call.get("from") or "").replace("client:", "")
    return REP_MAP.get(from_field[:7])


# ---- ALERT ------------------------------------------------------------------
def send_alert(rep, pattern, stats):
    """Post a structured payload to the alert webhook (Slack DM scenario)."""
    title_map = {
        "VOICEMAIL_WALL": f"🚨 {rep} — voicemail wall",
        "LOW_CONNECT_RATE": f"⚠️ {rep} — abnormally low connect rate",
        "NO_CONVERSATIONS": f"⚠️ {rep} — no real conversations",
        "LONG_GAP": f"⏸️ {rep} — silent during shift",
    }
    rate = (stats['connects'] / stats['dials'] * 100) if stats.get('dials') else 0
    msg_map = {
        "VOICEMAIL_WALL": (
            f"{rep} has dialed {stats['dials']} times in the last "
            f"{VOICEMAIL_WALL_WINDOW}h with *zero* calls ≥30s. "
            "Possible connection issue, autodialer problem, or working off-system. "
            "Worth a quick check-in: is everything OK on her end?"
        ),
        "LOW_CONNECT_RATE": (
            f"{rep} has made {stats['dials']} dials in the last "
            f"{VOICEMAIL_WALL_WINDOW}h but only {stats['connects']} real conversations "
            f"({rate:.1f}% connect rate). "
            "For comparison, healthy reps run ~20-25%. "
            "Possible voicemail-only dialing list, dialer config issue, or list quality problem."
        ),
        "NO_CONVERSATIONS": (
            f"{rep} has dialed {stats['dials']} times in the last "
            f"{NO_CONVERSATIONS_WINDOW}h with no real conversations yet. "
            "Could just be a slow connect window — but worth watching."
        ),
        "LONG_GAP": (
            f"{rep} had real conversations earlier today but has been silent "
            f"for {LONG_GAP_HOURS}+ hours (zero dials). "
            "Either on break/done for the day or unaware of an issue."
        ),
    }
    payload = {
        "rep": rep,
        "pattern": pattern,
        "title": title_map.get(pattern, pattern),
        "message": msg_map.get(pattern, ""),
        "stats": stats,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if ALERT_WEBHOOK and "PLACEHOLDER" not in ALERT_WEBHOOK:
        try:
            http("POST", ALERT_WEBHOOK,
                 headers={"Content-Type": "application/json"},
                 data=json.dumps(payload).encode())
            log(f"  → ALERT sent: {pattern} for {rep}")
        except Exception as e:
            log(f"  → alert webhook failed ({e}); logged below")
    log(f"  ALERT [{pattern}] {rep}: {msg_map.get(pattern,'')}")


# ---- MAIN -------------------------------------------------------------------
def gather_window(hours):
    """Return {rep: {'dials': int, 'connects': int}} for the last N hours."""
    recs = list_recent_recordings(hours)
    by_rep = defaultdict(lambda: {"dials": 0, "connects": 0, "samples": []})
    for r in recs:
        rep = identify_rep(r)
        if not rep:
            continue
        dur = int(r.get("duration", "0") or 0)
        by_rep[rep]["dials"] += 1
        if dur >= MIN_CONVERSATION_DUR:
            by_rep[rep]["connects"] += 1
        by_rep[rep]["samples"].append(dur)
    return by_rep


def main():
    state = load_state()

    log("=== Anomaly scan ===")
    long_window = gather_window(VOICEMAIL_WALL_WINDOW)
    log(f"4h window: {dict((r,(s['dials'],s['connects'])) for r,s in long_window.items())}")

    short_window = gather_window(NO_CONVERSATIONS_WINDOW)
    log(f"2h window: {dict((r,(s['dials'],s['connects'])) for r,s in short_window.items())}")

    today_window = gather_window(12)  # for LONG_GAP we need to know today's earlier activity
    log(f"12h window: {dict((r,(s['dials'],s['connects'])) for r,s in today_window.items())}")

    # ---- Pattern checks -----------------------------------------------------
    for rep in REP_MAP.values():
        long_stats = long_window.get(rep, {"dials": 0, "connects": 0})
        short_stats = short_window.get(rep, {"dials": 0, "connects": 0})
        today_stats = today_window.get(rep, {"dials": 0, "connects": 0})

        long_rate = (long_stats["connects"] / long_stats["dials"]) if long_stats["dials"] else 0

        # 1. VOICEMAIL_WALL — many dials, zero connects
        if (long_stats["dials"] >= VOICEMAIL_WALL_DIALS
                and long_stats["connects"] == 0
                and not already_alerted_today(state, rep, "VOICEMAIL_WALL")):
            send_alert(rep, "VOICEMAIL_WALL", long_stats)
            mark_alerted(state, rep, "VOICEMAIL_WALL")

        # 2. LOW_CONNECT_RATE — many dials, abnormally low connect rate (Julie's pattern)
        elif (long_stats["dials"] >= LOW_CONNECT_RATE_DIALS
                and long_rate < LOW_CONNECT_RATE_THRESHOLD
                and not already_alerted_today(state, rep, "LOW_CONNECT_RATE")):
            send_alert(rep, "LOW_CONNECT_RATE", long_stats)
            mark_alerted(state, rep, "LOW_CONNECT_RATE")

        # 3. NO_CONVERSATIONS — short window, zero connects
        elif (short_stats["dials"] >= NO_CONVERSATIONS_DIALS
                and short_stats["connects"] == 0
                and not already_alerted_today(state, rep, "NO_CONVERSATIONS")):
            send_alert(rep, "NO_CONVERSATIONS", short_stats)
            mark_alerted(state, rep, "NO_CONVERSATIONS")

        # 3. LONG_GAP — had connects earlier today but no dials in last 2h
        if (today_stats["connects"] >= 3                    # had real activity earlier
                and short_stats["dials"] == 0                # but silent now
                and not already_alerted_today(state, rep, "LONG_GAP")):
            send_alert(rep, "LONG_GAP", today_stats)
            mark_alerted(state, rep, "LONG_GAP")

    save_state(state)
    log("=== Scan complete ===")


if __name__ == "__main__":
    main()
