#!/usr/bin/env python3
"""
AGA Anomaly Detector — runs hourly via GitHub Actions.

Schedule-aware. Each rep has a known shift block this week (see _schedule.py).
Patterns are evaluated against shift status so we don't ping reps who are off.

Patterns:
  1. VOICEMAIL_WALL    — 20+ dials in 4h with 0 conversations ≥30s
  2. LOW_CONNECT_RATE  — 30+ dials with <5% connect rate (Julie's pattern)
  3. NO_CONVERSATIONS  — 5+ dials in 2h with 0 conversations
  4. LONG_GAP          — connects earlier today + silent for 2h+ DURING SHIFT
                         (won't fire if rep is off-shift)

Each fired alert posts a fully-formatted Slack message via the alert webhook.
The message includes shift context, last-active time, benchmark + suggested action.
"""
import json
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from email.utils import parsedate_to_datetime
from pathlib import Path

# Add scripts dir to path then import shared config + schedule
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import (TWILIO_ACCOUNT, TWILIO_AUTH, REP_MAP, ALERT_WEBHOOK, SHIFT_OVERRIDE_API)
from _schedule import EST, shift_status, shift_label_for_rep, BLOCKS, block_for_rep

STATE_FILE = Path.home() / ".aga-anomaly-state"
LOG_DIR = Path.home() / "Library/Logs/aga-call-scorer"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Thresholds
VOICEMAIL_WALL_DIALS = 20
VOICEMAIL_WALL_WINDOW = 4
LOW_CONNECT_RATE_DIALS = 30
LOW_CONNECT_RATE_THRESHOLD = 0.05
NO_CONVERSATIONS_DIALS = 5
NO_CONVERSATIONS_WINDOW = 2
LONG_GAP_HOURS = 2
MIN_CONVERSATION_DUR = 30  # seconds

# Healthy connect-rate benchmark (for the alert message)
BENCHMARK_LO = 20
BENCHMARK_HI = 25


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


def fmt_time_ago(dt_utc):
    """e.g. '2h 10m ago' from a UTC datetime."""
    if not dt_utc:
        return "—"
    delta = datetime.now(timezone.utc) - dt_utc
    mins = int(delta.total_seconds() // 60)
    if mins < 1: return "just now"
    if mins < 60: return f"{mins}m ago"
    h = mins // 60
    m = mins % 60
    return f"{h}h {m}m ago" if m else f"{h}h ago"


def fmt_clock(dt_utc):
    """e.g. '1:20 PM ET' from a UTC datetime."""
    if not dt_utc:
        return "—"
    return dt_utc.astimezone(EST).strftime("%-I:%M %p ET")


# ---- TWILIO -----------------------------------------------------------------
def fetch(url):
    _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
    return json.loads(body)


def list_recent_recordings(hours):
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
    if not sid: return None
    try:
        return fetch(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}/Calls/{sid}.json")
    except urllib.error.HTTPError:
        return None


def identify_rep(recording):
    call = get_call(recording.get("call_sid"))
    parent = get_call(call.get("parent_call_sid")) if call else None
    from_field = ""
    if parent:
        from_field = (parent.get("from") or "").replace("client:", "")
    if not from_field and call:
        from_field = (call.get("from") or "").replace("client:", "")
    return REP_MAP.get(from_field[:7])


def fetch_shift_overrides():
    """Fetch the shift overrides map from the Shift Overrides API.
    Returns {} if the API isn't configured or unreachable."""
    if not SHIFT_OVERRIDE_API:
        return {}
    try:
        _, body = http("POST", SHIFT_OVERRIDE_API,
                       headers={"Content-Type": "application/json"}, data=b"{}")
        arr = json.loads(body) or []
        return {f"{o.get('rep','')}_{o.get('week','')}": o.get('block')
                for o in arr if o.get('rep') and o.get('week') and o.get('block')}
    except Exception as e:
        log(f"  warning: failed to fetch shift overrides ({e})")
        return {}


# ---- ALERT FORMATTING ------------------------------------------------------
def build_alert_message(rep, pattern, stats, last_active_utc, overrides=None):
    """Build the rich Slack message. Returns dict suitable for the alert webhook."""
    now_est = datetime.now(EST)
    block = block_for_rep(rep, now_est.date(), overrides)
    shift_lbl = shift_label_for_rep(rep, overrides=overrides) or "—"
    status = shift_status(rep, now_est, overrides)

    shift_phrase_map = {
        "on_shift": f"on shift now ({shift_lbl} ET)",
        "before_shift": f"shift hasn't started ({shift_lbl} ET)",
        "after_shift": f"shift ended ({shift_lbl} ET)",
        "off_day": "off today",
    }
    shift_phrase = shift_phrase_map.get(status, "—")
    last_active_phrase = (
        f"Last active: {fmt_clock(last_active_utc)} ({fmt_time_ago(last_active_utc)})"
        if last_active_utc else "Last active: no calls yet today"
    )

    rate = (stats['connects'] / stats['dials'] * 100) if stats.get('dials') else 0

    if pattern == "VOICEMAIL_WALL":
        title = f"🚨 {rep} — Voicemail Wall"
        body = (
            f"*{stats['dials']} dials / {stats['connects']} conversations* "
            f"(last {VOICEMAIL_WALL_WINDOW}h)\n"
            f"Connect rate: *0%* · Benchmark: {BENCHMARK_LO}–{BENCHMARK_HI}%\n"
            f"{last_active_phrase} · {shift_phrase}\n"
            f"_Likely issue:_ dialer down, connection problem, or working off-system\n"
            f"*👉 Action:* check in with {rep} now"
        )
    elif pattern == "LOW_CONNECT_RATE":
        emoji = "🔴"
        title = f"{emoji} {rep} — Low Connect Rate ({rate:.1f}%)"
        body = (
            f"*{stats['dials']} dials / {stats['connects']} conversations* "
            f"(last {VOICEMAIL_WALL_WINDOW}h)\n"
            f"Benchmark: {BENCHMARK_LO}–{BENCHMARK_HI}%\n"
            f"{last_active_phrase} · {shift_phrase}\n"
            f"_Likely issue:_ list quality / dialer config\n"
            f"*👉 Action:* message {rep}, ask about list/dialer"
        )
    elif pattern == "NO_CONVERSATIONS":
        title = f"⚠️ {rep} — No Conversations Yet"
        body = (
            f"*{stats['dials']} dials / 0 conversations* (last {NO_CONVERSATIONS_WINDOW}h)\n"
            f"{last_active_phrase} · {shift_phrase}\n"
            f"_Likely issue:_ slow connect window, or just bad luck — watch but don't act yet\n"
            f"*👉 Action:* keep an eye on it for the next hour"
        )
    elif pattern == "LONG_GAP":
        title = f"🟡 {rep} — No Activity ({fmt_time_ago(last_active_utc)})"
        body = (
            f"Had {stats['connects']} real conversations earlier today, "
            f"but no dials in the last {LONG_GAP_HOURS}h.\n"
            f"{last_active_phrase} · {shift_phrase}\n"
            f"_Likely:_ on break, or done for the day — confirm don't accuse\n"
            f"*👉 Action:* quick check-in: \"everything OK?\""
        )
    else:
        title = f"{rep} — {pattern}"
        body = ""

    return {
        "rep": rep,
        "pattern": pattern,
        "title": title,
        "message": body,
        "shift_block": block,
        "shift_label": shift_lbl,
        "shift_status": status,
        "last_active_utc": last_active_utc.isoformat() if last_active_utc else None,
        "stats": stats,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def send_alert(payload):
    if ALERT_WEBHOOK and "PLACEHOLDER" not in ALERT_WEBHOOK:
        try:
            http("POST", ALERT_WEBHOOK,
                 headers={"Content-Type": "application/json"},
                 data=json.dumps(payload).encode())
            log(f"  → ALERT sent: {payload['pattern']} for {payload['rep']}")
        except Exception as e:
            log(f"  → alert webhook failed ({e})")
    log(f"  ALERT [{payload['pattern']}] {payload['rep']}")


# ---- MAIN -------------------------------------------------------------------
def gather_window(hours):
    """Return per-rep activity in the last N hours, including last_active timestamp."""
    recs = list_recent_recordings(hours)
    by_rep = defaultdict(lambda: {"dials": 0, "connects": 0, "last_active": None})
    for r in recs:
        rep = identify_rep(r)
        if not rep:
            continue
        dur = int(r.get("duration", "0") or 0)
        by_rep[rep]["dials"] += 1
        if dur >= MIN_CONVERSATION_DUR:
            by_rep[rep]["connects"] += 1
        # Track most recent recording timestamp
        try:
            ts = parsedate_to_datetime(r.get("date_created", "")).astimezone(timezone.utc)
            if not by_rep[rep]["last_active"] or ts > by_rep[rep]["last_active"]:
                by_rep[rep]["last_active"] = ts
        except Exception:
            pass
    return by_rep


def main():
    state = load_state()
    overrides = fetch_shift_overrides()
    now_est = datetime.now(EST)
    log(f"=== Anomaly scan @ {now_est.strftime('%a %Y-%m-%d %H:%M %Z')} ===")
    if overrides:
        log(f"Shift overrides loaded: {overrides}")

    # Show this week's shifts for context
    log("This week's shifts:")
    for rep in REP_MAP.values():
        b = block_for_rep(rep, now_est.date(), overrides)
        s = shift_status(rep, now_est, overrides)
        log(f"  {rep:<10} block {b} ({BLOCKS[b]['label']}) — currently {s}")

    long_window = gather_window(VOICEMAIL_WALL_WINDOW)
    short_window = gather_window(NO_CONVERSATIONS_WINDOW)
    today_window = gather_window(12)

    for rep in REP_MAP.values():
        long_stats = long_window.get(rep, {"dials": 0, "connects": 0, "last_active": None})
        short_stats = short_window.get(rep, {"dials": 0, "connects": 0, "last_active": None})
        today_stats = today_window.get(rep, {"dials": 0, "connects": 0, "last_active": None})
        last_active = (long_stats.get("last_active") or today_stats.get("last_active"))

        rep_status = shift_status(rep, now_est, overrides)
        long_rate = (long_stats["connects"] / long_stats["dials"]) if long_stats["dials"] else 0

        # 1. VOICEMAIL_WALL — fires regardless of shift (always relevant)
        if (long_stats["dials"] >= VOICEMAIL_WALL_DIALS
                and long_stats["connects"] == 0
                and not already_alerted_today(state, rep, "VOICEMAIL_WALL")):
            payload = build_alert_message(rep, "VOICEMAIL_WALL", long_stats, last_active, overrides)
            send_alert(payload)
            mark_alerted(state, rep, "VOICEMAIL_WALL")
            continue

        # 2. LOW_CONNECT_RATE — fires regardless of shift
        if (long_stats["dials"] >= LOW_CONNECT_RATE_DIALS
                and long_rate < LOW_CONNECT_RATE_THRESHOLD
                and not already_alerted_today(state, rep, "LOW_CONNECT_RATE")):
            payload = build_alert_message(rep, "LOW_CONNECT_RATE", long_stats, last_active, overrides)
            send_alert(payload)
            mark_alerted(state, rep, "LOW_CONNECT_RATE")
            continue

        # 3. NO_CONVERSATIONS — only DURING shift
        if (rep_status == "on_shift"
                and short_stats["dials"] >= NO_CONVERSATIONS_DIALS
                and short_stats["connects"] == 0
                and not already_alerted_today(state, rep, "NO_CONVERSATIONS")):
            payload = build_alert_message(rep, "NO_CONVERSATIONS", short_stats, last_active, overrides)
            send_alert(payload)
            mark_alerted(state, rep, "NO_CONVERSATIONS")
            continue

        # 4. LONG_GAP — only DURING shift (don't ping for breaks/off-hours)
        if (rep_status == "on_shift"
                and today_stats["connects"] >= 3
                and short_stats["dials"] == 0
                and not already_alerted_today(state, rep, "LONG_GAP")):
            payload = build_alert_message(rep, "LONG_GAP", today_stats, last_active, overrides)
            send_alert(payload)
            mark_alerted(state, rep, "LONG_GAP")

    save_state(state)
    log("=== Scan complete ===")


if __name__ == "__main__":
    main()
