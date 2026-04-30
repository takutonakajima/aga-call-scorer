#!/usr/bin/env python3
"""
AGA Top Call of the Week — runs Mondays at 8:05am ET (right after weekly_digest).

Picks the highest-scoring real conversation from the past week, downloads the
MP3 from Twilio, saves it into the Netlify deploy folder, generates a Gemini
"why this worked" + "try this" analysis, and writes the metadata to data store
96069 (Top Call Of Week).

The dashboard then renders a "🏆 Top Call This Week" card with audio playback.
"""
import base64
import json
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add scripts dir to path then import shared config
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import (TWILIO_ACCOUNT, TWILIO_AUTH, GEMINI_KEY, REP_MAP,
                     SCORE_INGEST, SCORE_API, DIALS_INGEST, TIPS_INGEST,
                     TOPCALL_INGEST, ALERT_WEBHOOK)
from zoneinfo import ZoneInfo







# Where MP3s get saved. Netlify deploys this folder, so files become public assets.
AUDIO_DIR = Path("/Users/juanarango/Desktop/Call Coaching AI/audio")

EST = ZoneInfo("America/New_York")
LOG_DIR = Path.home() / "Library/Logs/aga-call-scorer"
LOG_DIR.mkdir(parents=True, exist_ok=True)

MIN_DURATION_FOR_TOP = 60  # don't pick a 30-second voicemail-bait — must be at least 1 min


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http(method, url, headers=None, data=None, timeout=180):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def this_week_start_est():
    now = datetime.now(EST)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")


def last_week_start_est():
    now = datetime.now(EST)
    last_monday = now - timedelta(days=now.weekday() + 7)
    return last_monday.strftime("%Y-%m-%d")


def fetch_all_scores():
    _, body = http("POST", SCORE_API, headers={"Content-Type": "application/json"}, data=b"{}")
    return json.loads(body)


def download_mp3(recording_sid):
    """MP3 is much smaller than WAV (~20% the size). Twilio serves both."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT}/Recordings/{recording_sid}.mp3"
    _, body = http("GET", url, headers={"Authorization": TWILIO_AUTH}, timeout=60)
    return body


ANALYZE_PROMPT = """You are a senior sales coach for a medical-spa booking center.
Below is the highest-scoring rep call from this past week. Listen carefully.

Your job: write a peer-learning teardown for the OTHER reps on the team to learn from.

Return ONLY raw JSON with these two fields:

{
  "why_it_worked": "3-5 sentences. Name the 2-3 specific TECHNIQUES the rep used that made this call score high. Be concrete and quote a moment if you can.",
  "try_this": "2-3 sentences. Tell the OTHER reps exactly what to take from this call and try on their next call this week. Make it directly actionable."
}

Do not name the prospect.
"""


def gemini_analyze(audio_bytes):
    b64 = base64.b64encode(audio_bytes).decode("ascii")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    )
    payload = {
        "contents": [{"parts": [
            {"inlineData": {"mimeType": "audio/mpeg", "data": b64}},
            {"text": ANALYZE_PROMPT},
        ]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4096,
            "responseMimeType": "application/json",
        },
    }
    for attempt in range(3):
        try:
            _, body = http(
                "POST", url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload).encode(),
            )
            resp = json.loads(body)
            txt = resp["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise


def post_top_call(payload):
    _, _ = http(
        "POST", TOPCALL_INGEST,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )


def main():
    import sys
    target_week = this_week_start_est() if "--current" in sys.argv else last_week_start_est()
    log(f"Picking top call for week {target_week}")

    scores = fetch_all_scores()
    eligible = [
        c for c in scores
        if c.get("week") == target_week
        and (c.get("rep") or "").strip()
        and not (c.get("rep","")).startswith("+")
        and int(c.get("duration_seconds") or 0) >= MIN_DURATION_FOR_TOP
        and c.get("recording_sid")
    ]
    log(f"Eligible calls (>={MIN_DURATION_FOR_TOP}s, real rep): {len(eligible)}")
    if not eligible:
        log("No eligible calls. Skipping.")
        return

    # Pick highest score, tiebreak by longest duration (more substance)
    top = max(eligible, key=lambda c: (int(c.get("score") or 0), int(c.get("duration_seconds") or 0)))
    log(f"Top call: rep={top['rep']} score={top['score']} duration={top.get('duration_seconds')}s sid={top['recording_sid']}")

    # Download MP3 + save to Netlify audio folder
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    audio_filename = f"{top['recording_sid']}.mp3"
    out_path = AUDIO_DIR / audio_filename
    mp3 = download_mp3(top["recording_sid"])
    out_path.write_bytes(mp3)
    log(f"Saved audio ({len(mp3)//1024} KB) to {out_path}")

    # Optional: clean up old audio files (keep only this week's + last week's)
    keep = {audio_filename}
    for f in AUDIO_DIR.glob("*.mp3"):
        if f.name not in keep and (datetime.now() - datetime.fromtimestamp(f.stat().st_mtime)).days > 21:
            log(f"Removing old audio: {f.name}")
            f.unlink()

    # Gemini analysis
    log("Generating Gemini analysis...")
    analysis = gemini_analyze(mp3) or {}

    payload = {
        "week": target_week,
        "rep": top["rep"],
        "score": top["score"],
        "call_date": top.get("date", ""),
        "duration_seconds": int(top.get("duration_seconds") or 0),
        "audio_filename": audio_filename,
        "summary": top.get("summary", ""),
        "why_it_worked": analysis.get("why_it_worked", ""),
        "try_this": analysis.get("try_this", ""),
        "recording_sid": top["recording_sid"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    post_top_call(payload)
    log(f"Top call published to data store. why_it_worked: {payload['why_it_worked'][:80]}...")
    log("Done.")


if __name__ == "__main__":
    main()
