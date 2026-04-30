#!/usr/bin/env python3
"""
AGA Call Scorer — Twilio recording → Gemini 2.5 Flash → Make.com data store 95103.

Runs every 15 min via launchd. Scores any recording >=30s that hasn't been
processed before. State tracked in ~/.aga-scored-sids.

Fields written match what scenario 4886450 ("03 - Call Coaching Data API")
reads to power the Netlify rep dashboard.
"""
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
    """Build the 'seen' set from the live Score API — the data store is the
    source of truth, no local state file needed. Falls back to local file
    if the API is unavailable (e.g. running in dev). Stateless = ideal for CI."""
    try:
        _, body = http("POST", SCORE_API, headers={"Content-Type": "application/json"}, data=b"{}")
        records = json.loads(body)
        sids = set(r.get("recording_sid", "") for r in records if r.get("recording_sid"))
        log(f"Loaded {len(sids)} already-scored SIDs from Score API")
        return sids
    except Exception as e:
        log(f"  warning: could not fetch from Score API ({e}); falling back to local file")
        return set(STATE_FILE.read_text().split()) if STATE_FILE.exists() else set()


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
