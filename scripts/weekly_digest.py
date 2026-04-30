#!/usr/bin/env python3
"""
AGA Weekly Digest — Mondays 8am ET via launchd.

For each rep with calls in the past week:
  1. Pull all live-conversation scores from data store 95103 (via Score API)
  2. Send the week's summaries + improvement notes to Gemini
  3. Ask Gemini to synthesize 3 actionable coaching themes
  4. POST to Weekly Tips Ingest webhook → data store 96036
  5. (Optional) Slack DM Sophia + Juan with all reps' tips

The rep dashboard fetches from the Weekly Tips API and shows the synthesized
tips at the top of the page (replacing the client-side keyword clustering).
"""
import json
import time
import urllib.error
import urllib.request
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





LOG_DIR = Path.home() / "Library/Logs/aga-call-scorer"
LOG_DIR.mkdir(parents=True, exist_ok=True)

EST = ZoneInfo("America/New_York")  # auto-handles EST/EDT


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def http(method, url, headers=None, data=None, timeout=120):
    req = urllib.request.Request(url, method=method, data=data)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def this_week_start_est():
    """Monday of current week, EST, YYYY-MM-DD."""
    now = datetime.now(EST)
    monday = now - timedelta(days=now.weekday())
    return monday.strftime("%Y-%m-%d")


def last_week_start_est():
    """Monday of last week (the one we're digesting)."""
    now = datetime.now(EST)
    last_monday = now - timedelta(days=now.weekday() + 7)
    return last_monday.strftime("%Y-%m-%d")


def fetch_all_scores():
    _, body = http("POST", SCORE_API, headers={"Content-Type": "application/json"}, data=b"{}")
    return json.loads(body)


SYNTH_PROMPT = """You are a senior sales coach for a medical-spa appointment booking center.
Below are THIS WEEK'S calls for ONE specific booking rep. For each call you have:
- the date the call happened
- the call's overall score (1-10) and per-dimension scores
- a 2-3 sentence summary of what happened on the call
- the rep's TOP_STRENGTH on that call
- the rep's TOP_IMPROVEMENT on that call

Synthesize 3 PERSONALIZED COACHING MODULES that will move this rep's number this week.

EACH module MUST contain these 4 sections, in this order:
  THEME: Name the recurring pattern (something showing up in 2+ calls).
  THIS WEEK: Quote 2-3 SPECIFIC moments from this rep's actual calls — reference the day.
             Use the format: "On [Tuesday], you [what happened] — instead of [what you did], you could have [better move]."
  PRACTICE: One concrete drill the rep can do this week (role-play scenario, 3-step script to memorize, phrase library, etc.).
  WATCH: A YouTube search query with a specific creator. Pick from these when relevant:
    - Opening: "Cold call opening Patrick Dang"  |  "Phone sales opener Sales Insights Lab"
    - Discovery: "Chris Voss tactical empathy"  |  "SPIN selling discovery questions"
    - Pitch Clarity: "Pitch Anything Oren Klaff"  |  "How to pitch without sounding salesy Jeb Blount"
    - Objection Handling: "Chris Voss labeling objections"  |  "Mirror technique sales objections"  |  "Top sales objections Grant Cardone"
    - Closing: "Assumptive close Tom Hopkins"  |  "Soft close technique"
    - Tone & Energy: "Vocal energy phone sales"  |  "Sound confident on the phone"

Rules:
- Order the 3 modules from HIGHEST leverage (will move the score most) to lowest.
- Speak DIRECTLY to the rep ("You did...", "Try this:...", "When a prospect says X...").
- Do NOT name specific prospects or contacts. Reference dates only.
- Each module should be 5-8 sentences total across the 4 sections.
- Use plain text. Section labels (THEME:, THIS WEEK:, PRACTICE:, WATCH:) on their own lines, blank line between sections.

Return ONLY raw JSON, no markdown:
{"tip1":"THEME: ...\\n\\nTHIS WEEK: ...\\n\\nPRACTICE: ...\\n\\nWATCH: ...","tip2":"...","tip3":"..."}

The rep's call notes from this week:
"""


def fmt_call_for_prompt(c):
    """Build one call's entry in the prompt — full context for Gemini."""
    try:
        date = c.get("date", "")[:10]
    except Exception:
        date = ""
    score = c.get("score", "?")
    return (
        f"--- Call on {date} (score {score}/10) ---\n"
        f"  scores: opening={c.get('opening',0)} discovery={c.get('discovery',0)} "
        f"pitch={c.get('pitch',0)} objections={c.get('objection',0)} "
        f"closing={c.get('closing',0)} tone={c.get('tone',0)}\n"
        f"  SUMMARY: {c.get('summary','')}\n"
        f"  TOP_STRENGTH: {c.get('strength','')}\n"
        f"  TOP_IMPROVEMENT: {c.get('improvement','')}\n"
    )


def synthesize_tips(rep, calls):
    """Gemini-synthesize 3 personalized coaching modules from a week's call records."""
    if not calls:
        return None
    blocks = [fmt_call_for_prompt(c) for c in calls if c.get("summary") or c.get("improvement")]
    if not blocks:
        return None
    text = SYNTH_PROMPT + "\n".join(blocks)

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 8192,
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
            tips_text = resp["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(tips_text)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(8 * (attempt + 1))
                continue
            raise


def post_tips(rep, week, tips, call_count):
    payload = {
        "rep": rep,
        "week": week,
        "tip1": tips.get("tip1", ""),
        "tip2": tips.get("tip2", ""),
        "tip3": tips.get("tip3", ""),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "call_count": call_count,
    }
    status, _ = http(
        "POST", TIPS_INGEST,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )
    return status


def main():
    import sys
    # By default digest LAST week. Pass --current to digest current week (for testing).
    target_week = this_week_start_est() if "--current" in sys.argv else last_week_start_est()
    log(f"Digesting week starting {target_week}")

    all_scores = fetch_all_scores()
    log(f"Fetched {len(all_scores)} total scored calls from API")

    # Group by rep, filter to target week, exclude unknown reps
    by_rep = defaultdict(list)
    for c in all_scores:
        rep = (c.get("rep") or "").strip()
        if not rep or rep.startswith("+") or rep.startswith("Unknown"):
            continue
        if c.get("week") != target_week:
            continue
        by_rep[rep].append(c)

    log(f"Reps with calls this week: {dict((r, len(v)) for r, v in by_rep.items())}")

    results = {}
    for rep, calls in by_rep.items():
        try:
            log(f"  → Synthesizing for {rep} ({len(calls)} calls)...")
            tips = synthesize_tips(rep, calls)
            if not tips:
                log(f"    no improvement notes — skipping")
                continue
            status = post_tips(rep, target_week, tips, len(calls))
            results[rep] = {"call_count": len(calls), "tips": tips, "status": status}
            log(f"    OK  status={status}")
            log(f"    1) {tips.get('tip1','')[:90]}")
            log(f"    2) {tips.get('tip2','')[:90]}")
            log(f"    3) {tips.get('tip3','')[:90]}")
        except Exception as e:
            log(f"    FAIL {type(e).__name__}: {e}")

    log(f"Done. Generated tips for {len(results)} reps.")


if __name__ == "__main__":
    main()
