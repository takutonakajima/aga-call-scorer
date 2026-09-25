#!/usr/bin/env python3
"""
AGA Call Scorer — Twilio recording → Gemini 2.5 Flash → Make.com data store 95103.

RETIRED 2026-09-25 — see the guard below. Nothing runs this.

Both of its schedulers are off: the local launchd job com.aga.callscorer was
disabled 2026-06-02, and the GitHub Actions workflow has been disabled_manually
since 2026-06-15. docs/SCHEDULED_ROUTINES.md is correct that an edge function
"replaced local com.aga.callscorer (score_calls.py)" — `call-scorer` now does
this work on pg_cron job 27.

History worth keeping, because this paragraph was wrong twice in two days: a
2026-09-24 audit retired the file, then reverted on finding score-calls.yml and
wrote here that CI still ran it ~69x/day. That was never checked against the
Actions API — the workflow was already disabled. The .yml file's cron: lines
look active whether or not the workflow is. Check STATE, not the file.

Scores any recording >=30s that hasn't been processed before.
State tracked in ~/.aga-scored-sids.

Fields written match what scenario 4886450 ("03 - Call Coaching Data API")
reads to power the Netlify rep dashboard.
"""

# ─── RETIRED 2026-09-25 — DO NOT RUN, DO NOT RE-ENABLE ───────────────────────
# Replaced by the `call-scorer` Supabase edge function (pg_cron job 27, */5).
# Evidence it is live: 283 successful invocations in the last 24h.
#
# Its GitHub Actions workflow is `disabled_manually` and has not executed since
# 2026-06-15. That state is NOT visible in the .yml file — the cron: lines in
# there look perfectly active, which is exactly what fooled an audit on
# 2026-09-25 into reporting that this script was still running and wasting
# money. Workflow STATE lives in the Actions API, not the file:
#     curl -s https://api.github.com/repos/takutonakajima/aga-call-scorer/actions/workflows
#
# Kept in-tree for reference and for diffing against the edge function. If you
# genuinely need to run it by hand:  AGA_RUN_SUPERSEDED=1 python3 scripts/score_calls.py
import os as _os, sys as _sys
if _os.environ.get("AGA_RUN_SUPERSEDED") != "1":
    _sys.exit(
        "REFUSING TO RUN — retired 2026-09-25, replaced by the `call-scorer` edge function.\n"
        "Re-enabling this alongside the edge function would double-process.\n"
        "Set AGA_RUN_SUPERSEDED=1 only if you truly mean to run the old path."
    )
# ─────────────────────────────────────────────────────────────────────────────
import base64
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from pathlib import Path

# Add scripts dir to path then import shared config
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import (TWILIO_ACCOUNT, TWILIO_AUTH, GEMINI_KEY, REP_MAP,
                     SCORE_INGEST, SCORE_API, DIALS_INGEST, TIPS_INGEST,
                     TOPCALL_INGEST, ALERT_WEBHOOK)

# ---- CONFIG -----------------------------------------------------------------





MIN_DURATION_SECONDS = 30
GEMINI_MAX_RETRIES = 3
GEMINI_RETRY_DELAY = 8

# coaching-api (Supabase) — the live replacement for the retired Make.com read
# endpoints. The rep dashboards already read through this via the Netlify gate;
# see Call Coaching AI/netlify/functions/gate.js, which sends the same header.
# Default matches that gate's COACHING_API_BASE so CI only has to supply the secret.
COACHING_API = os.environ.get(
    "COACHING_API_BASE",
    "https://avknogjtwgywdxhsfkji.supabase.co/functions/v1/coaching-api",
).rstrip("/")
COACHING_READ_SECRET = os.environ.get("COACHING_READ_SECRET", "")

STATE_FILE = Path.home() / ".aga-scored-sids"
LOG_DIR = Path.home() / "Library/Logs/aga-call-scorer"
LOG_DIR.mkdir(parents=True, exist_ok=True)



# EST/EDT — week_start uses the same anchor as the booking system
EST = ZoneInfo("America/New_York")  # auto-handles EST/EDT

PROMPT = (
    "You are a call quality analyst for a medical spa appointment booking center. "
    "Score this recording of a call between a booking rep and a prospect. "
    "Return ONLY raw JSON, no markdown, no code fences. Required fields: "
    "was_voicemail (boolean, true ONLY if the call hit a voicemail/auto-attendant/IVR/network message and NO live human ever picked up — in this case the rest of the fields can be 0/empty), "
    "was_live_conversation (boolean, true if a real human prospect answered and spoke with the rep), "
    "overall_score (integer 1-10), "
    "opening (integer 1-10, how the rep greets and frames the call), "
    "pitch_clarity (integer 1-10, how clearly the rep explains the offer/service), "
    "tone_and_energy (integer 1-10, warmth, confidence, pacing), "
    "discovery (integer 1-10, asking questions to understand prospect needs), "
    "objection_handling (integer 1-10, how the rep responds to hesitation), "
    "closing (integer 1-10, the ask to book / next step), "
    "rep_talked_percent (integer 0-100), "
    "booked_appointment (boolean, true if an appointment was confirmed on this call), "
    "summary (string, 2-3 sentences), "
    "top_strength (string, single sentence describing the strongest moment), "
    "top_improvement (string, single sentence on the most important thing to fix)."
)


# ---- HELPERS ----------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http(method, url, headers=None, data=None, timeout=180):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def load_state():
    """Build the 'seen' set of already-scored recording SIDs.

    Reads coaching-api (Supabase), NOT the old Make.com SCORE_API.

    Why this moved (2026-09-25): every Make.com READ endpoint this repo used is
    retired. SCORE_API returns 401 and the other six return 410 Gone — confirmed
    by backup_data.py, which has recorded exactly that in 91 of 91 snapshots. The
    data now lives in Postgres: public.call_scores held 8,304 rows with the newest
    written minutes before this change. coaching-api is what the rep dashboards
    already read through, and it was BUILT as a drop-in for the old shape — its
    aliasScore() deliberately re-emits `recording_sid`, `rep`, `date`, `score`
    with the comment "Scripts expect these short names from the old Make.com API
    shape". So this is the replacement finally being used, not a new integration.

    What this WOULD do if run: load_state() falls to an empty set (GitHub Actions
    has a fresh $HOME, so the local fallback file never exists), the
    `r["sid"] not in seen` filter matches nothing, and every recording in the
    batch is re-scored, re-paying Gemini each time. An earlier note here said
    that was happening ~69x/day; it was not — the workflow has been disabled
    since 2026-06-15. Fixed anyway so the trap is gone if anyone revives this.

    NOTE the 500-row cap on coaching-api/scores. That is fine here: main() only
    ever considers the latest 100 Twilio recordings, so 500 recent scores cover
    the comparison window several times over.
    """
    if not COACHING_API or not COACHING_READ_SECRET:
        # Fail loudly rather than silently returning {} — an empty seen-set is
        # indistinguishable from "nothing has been scored yet" and re-scores
        # everything. Missing config should look like missing config.
        log("  ERROR: COACHING_API_BASE / COACHING_READ_SECRET not set — cannot "
            "build the already-scored set")
        raise RuntimeError("coaching-api not configured")
    try:
        _, body = http("GET", f"{COACHING_API}/scores",
                       headers={"x-coaching-secret": COACHING_READ_SECRET})
        records = json.loads(body)
        if not isinstance(records, list):
            raise RuntimeError(f"unexpected payload: {str(records)[:120]}")
        sids = set(r.get("recording_sid", "") for r in records if r.get("recording_sid"))
        log(f"Loaded {len(sids)} already-scored SIDs from coaching-api")
        return sids
    except Exception as e:
        log(f"  warning: could not fetch from coaching-api ({e}); falling back to local file")
        fallback = set(STATE_FILE.read_text().split()) if STATE_FILE.exists() else set()
        if not fallback:
            # An EMPTY seen-set is the most dangerous possible default here: the filter
            # in main() is `r["sid"] not in seen`, so an empty set matches nothing and
            # EVERY recording >=30s in the latest 100 is re-scored — paying Gemini again
            # for calls already scored, ~69 CI runs/day.
            #
            # Not a rare edge: GitHub Actions gives each run a fresh $HOME, so
            # STATE_FILE never exists and this branch is reached on EVERY run whenever
            # the read API is unreachable. That was the steady state for months while
            # this pointed at the retired Make.com SCORE_API, and it stayed invisible
            # because ALERT_WEBHOOK was imported at the top of this file and never used.
            #
            # Behaviour deliberately unchanged: aborting would stop scoring altogether,
            # a bigger outage than duplicate scoring. Make it loud instead.
            log("  *** coaching-api UNREACHABLE AND NO LOCAL STATE — dedupe is INERT: "
                "every recording in this batch will be re-scored ***")
            try:
                http("POST", ALERT_WEBHOOK,
                     headers={"Content-Type": "application/json"},
                     data=json.dumps({
                         "rep": "_SYSTEM",
                         "pattern": "SCORER_DEDUPE_INERT",
                         "title": "Call scorer is re-scoring every call",
                         "message": (
                             f"load_state() could not reach coaching-api ({e}) and no local "
                             f"state file exists, so the already-scored filter is empty. Every "
                             f"recording >=30s in the latest 100 is re-scored on every run, "
                             f"re-paying Gemini each time. Check "
                             f"COACHING_READ_SECRET and COACHING_API_BASE."
                         ),
                     }).encode())
            except Exception as alert_err:
                log(f"  (could not send dedupe-inert alert: {alert_err})")
        return fallback


def save_state(sids):
    """No-op when running statelessly. Kept as a stub so existing call sites work.
    The data store IS the state — every successfully scored call is already in it."""
    pass


def to_iso8601(rfc2822):
    return parsedate_to_datetime(rfc2822).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def week_start_est(rfc2822):
    """Monday of the call's week, in EST. Format YYYY-MM-DD. Matches booking system."""
    dt = parsedate_to_datetime(rfc2822).astimezone(EST)
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


# ---- TWILIO -----------------------------------------------------------------
def list_recordings(page_size=100, days=None):
    """If days is given, paginate through all recordings created in last N days."""
    if days is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        url = (
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}"
            f"/Recordings.json?PageSize=200&DateCreated%3E={since}"
        )
        all_recs = []
        page = 0
        while url and page < 30:
            page += 1
            _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
            data = json.loads(body)
            all_recs.extend(data["recordings"])
            nxt = data.get("next_page_uri")
            url = f"https://api.twilio.com{nxt}" if nxt else None
        return all_recs
    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}"
        f"/Recordings.json?PageSize={page_size}"
    )
    _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
    return json.loads(body)["recordings"]


def get_call(call_sid):
    if not call_sid:
        return None
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}/Calls/{call_sid}.json"
    try:
        _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
        return json.loads(body)
    except urllib.error.HTTPError:
        return None


def download_wav(sid):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}/Recordings/{sid}.wav"
    _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH})
    return body


# ---- GEMINI -----------------------------------------------------------------
def score_with_gemini(wav_bytes):
    b64 = base64.b64encode(wav_bytes).decode("ascii")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    )
    payload = {
        "contents": [
            {"parts": [
                {"inlineData": {"mimeType": "audio/wav", "data": b64}},
                {"text": PROMPT},
            ]}
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    last_err = None
    for attempt in range(1, GEMINI_MAX_RETRIES + 1):
        try:
            _, body = http(
                "POST", url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload).encode(),
            )
            resp = json.loads(body)
            text = resp["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(text)
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504) and attempt < GEMINI_MAX_RETRIES:
                time.sleep(GEMINI_RETRY_DELAY * attempt)
                continue
            raise
    raise last_err


# ---- REP RESOLUTION ---------------------------------------------------------
def resolve_rep(parent_call, recording_call):
    candidates = []
    if parent_call:
        candidates.append((parent_call.get("from") or "").replace("client:", ""))
    candidates.append((recording_call.get("from") or "").replace("client:", ""))
    for c in candidates:
        if c[:7] in REP_MAP:
            return REP_MAP[c[:7]]
    for c in candidates:
        if c:
            return c
    return "Unknown"


def resolve_contact(parent_call, recording_call):
    """The prospect's number — what got dialed from the rep's leg.
    For rep dial-outs the recording leg's `to` is the prospect."""
    return (
        recording_call.get("to")
        or (parent_call.get("to") if parent_call else None)
        or "Unknown"
    )


# ---- WEBHOOK ----------------------------------------------------------------
def post_to_webhook(record):
    body = json.dumps(record).encode()
    status, resp = http(
        "POST", SCORE_INGEST,
        headers={"Content-Type": "application/json"},
        data=body,
    )
    return status, resp.decode(errors="replace")


# ---- MAIN -------------------------------------------------------------------
def main():
    import sys
    days = None
    if len(sys.argv) > 1 and sys.argv[1].startswith("--days="):
        days = int(sys.argv[1].split("=")[1])
        log(f"BACKFILL MODE: last {days} days")
    seen = load_state()
    recs = list_recordings(days=days) if days else list_recordings(100)
    long_recs = [
        r for r in recs
        if int(r.get("duration", "0") or 0) >= MIN_DURATION_SECONDS
        and r["sid"] not in seen
    ]
    log(f"Total: {len(recs)} | already scored: {len(seen)} | new >={MIN_DURATION_SECONDS}s: {len(long_recs)}")

    scored = failed = 0
    for r in long_recs:
        sid = r["sid"]
        call_sid = r["call_sid"]
        try:
            recording_call = get_call(call_sid) or {}
            parent_call = get_call(recording_call.get("parent_call_sid"))
            rep = resolve_rep(parent_call, recording_call)
            contact = resolve_contact(parent_call, recording_call)
            wav = download_wav(sid)
            score = score_with_gemini(wav)

            # Skip voicemails / no-live-conversation calls — don't pollute coaching scores
            if score.get("was_voicemail") or not score.get("was_live_conversation", True):
                seen.add(sid)
                log(f"  SKIP voicemail/no-connect {sid} rep={rep}")
                continue

            record = {
                "recording_sid": sid,
                "call_sid": call_sid,
                "rep_name": rep,
                "contact_name": contact,
                "call_date": to_iso8601(r["date_created"]),
                "week_start": week_start_est(r["date_created"]),
                "duration_seconds": int(r["duration"]),
                "overall_score": score.get("overall_score"),
                "opening": score.get("opening"),
                "pitch_clarity": score.get("pitch_clarity"),
                "tone_and_energy": score.get("tone_and_energy"),
                "discovery": score.get("discovery"),
                "objection_handling": score.get("objection_handling"),
                "closing": score.get("closing"),
                "rep_talked_percent": score.get("rep_talked_percent"),
                "booked_appointment": score.get("booked_appointment"),
                "summary": score.get("summary", ""),
                "top_strength": score.get("top_strength", ""),
                "top_improvement": score.get("top_improvement", ""),
            }

            status, resp = post_to_webhook(record)
            if status == 200:
                seen.add(sid)
                scored += 1
                log(f"  OK  {sid} score={record['overall_score']}/10 rep={rep}")
            else:
                failed += 1
                log(f"  FAIL webhook {status}: {resp[:200]} ({sid})")
        except Exception as e:
            failed += 1
            log(f"  FAIL {type(e).__name__}: {e} ({sid})")

    save_state(seen)
    log(f"Done. Scored: {scored} | Failed: {failed} | State: {len(seen)}")


if __name__ == "__main__":
    main()
