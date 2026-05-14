#!/usr/bin/env python3
"""
AGA #script-updates ‚Üí HARDCODED + Netlify sync  (v2 ‚Äî no Google Sheets for Q&A)

How it works:
  1. Polls Slack #script-updates for new ACTION: posts from Sophia.
  2. Parses each post (clinic, offer, Q&A, metadata).
  3. Downloads the current index.html from Netlify (the source of truth).
  4. Applies all changes directly to the HARDCODED JSON inside index.html:
       - New clinic  ‚Üí creates a new entry in HARDCODED
       - New offer   ‚Üí appends to the clinic's offers array
       - Q&A update  ‚Üí writes parsed Q-A pairs directly as JSON objects
       - Metadata    ‚Üí updates deposit, CRM URL, ops notes, etc.
  5. Deploys the modified index.html to Netlify (2-step manifest upload).
  6. Also writes offer metadata (NOT Q&A) to Google Sheet so the portal's
     active/inactive logic continues to work.
  7. DMs Sophia on Slack confirming the update is live.

Why this is reliable:
  - Q&A is stored as structured JSON in HARDCODED ‚Äî no flat-string encoding,
    no parseFAQ round-trip, no fuzzy offer matching needed.
  - One deploy per run batch ‚Äî if 3 posts come in, we apply all 3 then deploy once.
  - If Netlify deploy fails, the script errors loudly and Sophia gets a DM.
  - The Google Sheet is still updated for offer visibility (active=YES/NO,
    price, URLs) but is no longer involved in Q&A at all.

New GitHub Actions secrets required:
  NETLIFY_TOKEN    ‚Äî nfp_TW3wxCv4vsswfzFBVwwZmMzT5j5tWSUde6e8
  NETLIFY_SITE_ID  ‚Äî 972558c1-0f0f-47a4-b737-b8084e4c1c4d

Existing secrets still used:
  SLACK_BOT_TOKEN, SCRIPT_SYNC_STATE_GET, SCRIPT_SYNC_STATE_SET,
  GOOGLE_SHEETS_KEY (for metadata writes to sheet), ALERT_WEBHOOK_URL
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ‚îÄ‚îÄ Config ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
SLACK_BOT_TOKEN      = os.environ.get("SLACK_BOT_TOKEN", "")
NETLIFY_TOKEN        = os.environ.get("NETLIFY_TOKEN", "")
NETLIFY_SITE_ID      = os.environ.get("NETLIFY_SITE_ID", "")
NETLIFY_SITE_URL     = os.environ.get("NETLIFY_SITE_URL", "https://agacallcenter.netlify.app")
GOOGLE_SHEETS_KEY    = os.environ.get("GOOGLE_SHEETS_KEY", "")
SCRIPT_SYNC_STATE_GET = os.environ.get("SCRIPT_SYNC_STATE_GET", "")
SCRIPT_SYNC_STATE_SET = os.environ.get("SCRIPT_SYNC_STATE_SET", "")
ALERT_WEBHOOK        = os.environ.get("ALERT_WEBHOOK_URL", "")

CHANNEL_ID     = "C0AV8UBGC69"   # #script-updates
SOPHIA_USER_ID = "U0AUCP53T3R"
MAI_USER_ID    = "U0AV75DLE1X"  # Jeamai Beltran (Mai) — also posts ACTION updates (A2P + assets)
ALLOWED_USER_IDS = {SOPHIA_USER_ID, MAI_USER_ID}
SHEET_ID       = "1wZEQYV4RgjbWHrRhnw58DIiFGNrOM5-jlJQuNv3_nFw"
OFFERS_TAB     = "offers"
CLINICS_TAB    = "clinics"

DEFAULT_GOLDEN_RULE = (
    "DO NOT ask them to book until you've explained the promotion, the price, "
    "and answered ALL questions. Once you have answered all their concerns ‚Äî "
    "THEN book. You MUST explain the card on file / deposit rule word for word "
    "when booking."
)

HARDCODED_RE = re.compile(r'(const HARDCODED\s*=\s*)(\[.*?\])(;)', re.DOTALL)


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ‚îÄ‚îÄ Slack helpers ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
def slack_call(method, params=None, post=False):
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    if post:
        headers["Content-Type"] = "application/json; charset=utf-8"
        data = json.dumps(params or {}).encode()
        req = urllib.request.Request(url, headers=headers, data=data, method="POST")
    else:
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    if not body.get("ok"):
        raise RuntimeError(f"Slack {method} failed: {body}")
    return body


def fetch_messages_since(oldest_ts):
    """Fetch Slack messages newer than oldest_ts, in chronological order."""
    body = slack_call("conversations.history", {
        "channel": CHANNEL_ID,
        "oldest": oldest_ts,
        "limit": 200,
    })
    return list(reversed(body.get("messages", [])))


def slack_dm(user_id, text):
    if not SLACK_BOT_TOKEN:
        return
    try:
        slack_call("chat.postMessage",
                   {"channel": user_id, "text": text, "mrkdwn": True},
                   post=True)
    except Exception as e:
        log(f"  Slack DM failed: {e}")


# ‚îÄ‚îÄ State (last-processed message ts) ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
def get_last_ts():
    if not SCRIPT_SYNC_STATE_GET:
        return f"{time.time() - 1800:.6f}"
    try:
        with urllib.request.urlopen(SCRIPT_SYNC_STATE_GET, timeout=15) as r:
            payload = json.loads(r.read())
        return payload.get("last_ts") or f"{time.time() - 1800:.6f}"
    except Exception:
        return f"{time.time() - 1800:.6f}"


def set_last_ts(ts):
    if not SCRIPT_SYNC_STATE_SET:
        return
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                SCRIPT_SYNC_STATE_SET, method="POST",
                data=json.dumps({"last_ts": ts}).encode(),
                headers={"Content-Type": "application/json"}),
            timeout=15
        ).read()
    except Exception as e:
        log(f"  State set failed: {e}")


# ‚îÄ‚îÄ Parser ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
FIELD_RE = re.compile(
    r"^(ACTION|CLINIC|OFFER\s+NAME|PRICE|LANDING\s+PAGE|BOOKING\s+PAGE|DEPOSIT|"
    r"CRM\s+LINK|CRM\s+LOGIN|CRM\s+PASSWORD|NOTES|MACHINES|GOLDEN\s+RULE|FAQ)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def clean(v):
    """Strip Slack formatting: [brackets], <url>, *bold*, leading numbers."""
    if v is None:
        return ""
    v = v.strip()
    # Slack URL wrapping: <https://...> or <https://...|text>
    v = re.sub(r"<(https?://[^|>]+)(?:\|[^>]+)?>", r"\1", v)
    v = re.sub(r"<mailto:([^|>]+)(?:\|[^>]+)?>", r"\1", v)
    # Strip surrounding [brackets] if balanced
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1].strip()
    elif v.startswith("["):
        # Unbalanced ‚Äî strip only the opener
        v = v[1:].strip()
    # Strip *bold* markers
    v = v.strip("*").strip()
    return v


def faq_to_qanda(faq_block):
    """
    Parse Sophia's multi-line FAQ block into [{"q": ..., "a": ...}, ...].

    Handles all observed format variants:
      Q: [question]            ‚Äî standard
      Q: [*question*]          ‚Äî bold markers inside
      *Q: [question]*          ‚Äî bold markers outside
      Q; [question]            ‚Äî semicolon typo
      Q: [*1. question*]       ‚Äî numbered prefix
      A : [answer]             ‚Äî space before colon
      A: multi-line\\ncontinued ‚Äî answer spans multiple lines
    """
    if not faq_block or not faq_block.strip():
        return []

    pairs = []
    cur_q = None
    cur_a = []

    for raw in faq_block.splitlines():
        # Strip surrounding whitespace and outer asterisks
        line = raw.strip()
        line_stripped = re.sub(r"^\*+|\*+$", "", line).strip()
        if not line_stripped:
            continue

        # Q detection: Q: / Q; (semicolon typo), optional leading * / number
        q_m = re.match(r"^Q\s*[;:]\s*(.*)", line_stripped, re.IGNORECASE)
        # A detection: A: or A : (space before colon)
        a_m = re.match(r"^A\s*:\s*(.*)", line_stripped, re.IGNORECASE)

        if q_m:
            # Save previous pair first
            if cur_q and cur_a:
                pairs.append({"q": cur_q, "a": _join_answer(cur_a)})
            raw_q = clean(q_m.group(1).strip())
            # Strip leading number prefix like "1. " or "1) "
            raw_q = re.sub(r"^\d+[.)]\s*", "", raw_q).strip()
            # Strip remaining asterisks
            raw_q = raw_q.strip("*").strip()
            # Ensure ends with ?
            if raw_q and not raw_q.endswith("?"):
                raw_q += "?"
            cur_q = raw_q
            cur_a = []

        elif a_m:
            if cur_q is not None:
                part = clean(a_m.group(1).strip())
                if part:
                    cur_a.append(part)

        elif cur_q is not None and cur_a:
            # Continuation of previous answer
            part = clean(line_stripped)
            if part:
                cur_a.append(part)

    # Don't forget the last pair
    if cur_q and cur_a:
        pairs.append({"q": cur_q, "a": _join_answer(cur_a)})

    return pairs


def _join_answer(parts):
    text = " ".join(parts).strip()
    text = re.sub(r"\s+", " ", text)
    text = text.strip("*").strip()
    return text


def parse_action_post(text):
    """Parse a single ACTION:ADD/UPDATE Slack post into a normalized dict."""
    if not text or not text.lstrip().upper().startswith("ACTION"):
        return None

    matches = list(FIELD_RE.finditer(text))
    if not matches:
        return None

    fields = {}
    for i, m in enumerate(matches):
        key = re.sub(r"\s+", "_", m.group(1).upper().strip())
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        fields[key] = text[start:end].strip()

    if "ACTION" not in fields:
        return None

    faq_raw = fields.get("FAQ", "")

    return {
        "action":       clean(fields.get("ACTION", "")).upper(),
        "client_name":  clean(fields.get("CLINIC", "")),
        "offer_name":   clean(fields.get("OFFER_NAME", "")),
        "price":        clean(fields.get("PRICE", "")),
        "landing_page": clean(fields.get("LANDING_PAGE", "")),
        "booking_page": clean(fields.get("BOOKING_PAGE", "")),
        "deposit":      clean(fields.get("DEPOSIT", "")),
        "crm_url":      clean(fields.get("CRM_LINK", "")),
        "crm_login":    clean(fields.get("CRM_LOGIN", "")),
        "crm_password": clean(fields.get("CRM_PASSWORD", "")),
        "machines":     clean(fields.get("MACHINES", "")),
        "golden_rule":  clean(fields.get("GOLDEN_RULE", "")),
        "ops_notes":    clean(fields.get("NOTES", "")),
        "faq_raw":      faq_raw,         # raw block ‚Üí used for HARDCODED qanda
        "faq":          _faq_to_cell(faq_raw),  # flat string ‚Üí used for Sheet fallback
    }


def _faq_to_cell(faq_raw):
    """Convert faq_raw to the flat-string sheet format (kept as legacy fallback)."""
    qanda = faq_to_qanda(faq_raw)
    parts = []
    for item in qanda:
        a_safe = item["a"].replace("||", "/").replace(" | ", " ‚Äî ")
        parts.append(f"Q: {item['q']} | A: {a_safe}")
    return " || ".join(parts)


# ‚îÄ‚îÄ HARDCODED update ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def fetch_portal_html():
    """Download the current index.html from Netlify, bypassing the passcode gate
    via PORTAL_FETCH_TOKEN. Without the token the gate returns the ~1.5 KB login
    page instead of the 270 KB portal — which then crashes extract_hardcoded()."""
    fetch_token = os.environ.get("PORTAL_FETCH_TOKEN", "").strip()
    if not fetch_token:
        raise RuntimeError(
            "PORTAL_FETCH_TOKEN env var missing — gate will return login page instead of portal HTML")
    headers = {
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        # Send via THREE channels for resilience (Netlify sometimes filters Authorization):
        "Authorization": f"Bearer {fetch_token}",
        "X-Portal-Fetch-Token": fetch_token,
    }
    url = NETLIFY_SITE_URL.rstrip("/") + f"/?fetch_token={fetch_token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
    if len(body) < 5000 or "const HARDCODED" not in body:
        raise RuntimeError(
            f"Portal fetch returned {len(body)} bytes without HARDCODED block — "
            "PORTAL_FETCH_TOKEN may be wrong or gate bypass broke")
    return body


def extract_hardcoded(html):
    """Return (data_list, match) from the HARDCODED JSON in index.html."""
    m = HARDCODED_RE.search(html)
    if not m:
        raise RuntimeError("Could not find `const HARDCODED = [...]` in index.html")
    data = json.loads(m.group(2))
    return data, m


def replace_hardcoded(html, m, new_data):
    """Replace the HARDCODED JSON in html with new_data, return new html string."""
    new_json = json.dumps(new_data, ensure_ascii=False, separators=(",", ":"))
    return html[: m.start(2)] + new_json + html[m.end(2):]


def apply_to_hardcoded(data, parsed):
    """
    Apply one parsed ACTION post to the HARDCODED list (mutates in place).
    Returns a short description string of what changed.
    """
    clinic_name = parsed["client_name"]
    offer_name  = parsed["offer_name"]
    changed     = []

    # ‚îÄ‚îÄ Find or create clinic ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    clinic = next((c for c in data if norm(c.get("name", "")) == norm(clinic_name)), None)
    if clinic is None:
        clinic_id = re.sub(r"[^a-z0-9]+", "-", clinic_name.lower()).strip("-")
        clinic = {
            "id": clinic_id,
            "name": clinic_name,
            "location": "", "address": "",
            "deposit": 0,
            "goldenRule": DEFAULT_GOLDEN_RULE,
            "opsNotes": [],
            "offers": [],
            "crmUrl": "", "crmLogin": "", "crmPassword": "", "machines": "",
        }
        data.append(clinic)
        log(f"    ‚Üí New clinic created: {clinic_name}")
        changed.append("new-clinic")

    # ‚îÄ‚îÄ Update clinic-level metadata ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    if parsed.get("deposit"):
        dep_m = re.search(r"\d+", parsed["deposit"])
        if dep_m:
            clinic["deposit"] = int(dep_m.group())
            changed.append("deposit")

    for field, key in [("crm_url", "crmUrl"), ("crm_login", "crmLogin"),
                        ("crm_password", "crmPassword"), ("machines", "machines"),
                        ("golden_rule", "goldenRule")]:
        if parsed.get(field):
            clinic[key] = parsed[field]
            changed.append(key)

    if parsed.get("ops_notes"):
        note = parsed["ops_notes"]
        existing = clinic.get("opsNotes", [])
        if note not in existing:
            clinic["opsNotes"] = existing + [note]
            changed.append("opsNotes")

    if not offer_name:
        return f"{clinic_name}: clinic metadata ‚Üí {', '.join(changed) or 'no changes'}"

    # ‚îÄ‚îÄ Find or create offer (exact match ‚Äî no fuzzy) ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    offer = next(
        (o for o in clinic.get("offers", []) if norm(o.get("name", "")) == norm(offer_name)),
        None
    )
    if offer is None:
        offer_id = re.sub(r"[^a-z0-9]+", "-", offer_name.lower()).strip("-")[:40]
        offer = {
            "id": offer_id, "name": offer_name,
            "price": "", "paymentNote": "", "depositOverride": None,
            "upgradeNote": "", "landingPage": "", "depositPage": "",
            "googleSheet": "", "qanda": [],
        }
        clinic.setdefault("offers", []).append(offer)
        log(f"    ‚Üí New offer created: {offer_name}")
        changed.append("new-offer")

    # ‚îÄ‚îÄ Update offer metadata ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    for field, key in [("price", "price"), ("landing_page", "landingPage"),
                        ("booking_page", "depositPage")]:
        if parsed.get(field):
            offer[key] = parsed[field]
            changed.append(key)

    # ‚îÄ‚îÄ Write Q&A directly as structured JSON ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    qanda = faq_to_qanda(parsed.get("faq_raw", ""))
    if qanda:
        offer["qanda"] = qanda
        changed.append(f"qanda({len(qanda)} Qs)")

    return f"{clinic_name} / {offer_name}: {', '.join(changed) or 'no changes'}"


# ‚îÄ‚îÄ Netlify deploy ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
def netlify_deploy(html_content):
    """
    Two-step Netlify manifest deploy.
      1. POST /deploys with SHA1 digest of index.html
      2. PUT file content if Netlify doesn't already have it cached
      3. Poll until state=ready
    Returns the live URL on success; raises RuntimeError on failure.
    """
    if not NETLIFY_TOKEN or not NETLIFY_SITE_ID:
        raise RuntimeError("NETLIFY_TOKEN or NETLIFY_SITE_ID not set")

    html_bytes = html_content.encode("utf-8") if isinstance(html_content, str) else html_content
    sha1 = hashlib.sha1(html_bytes).hexdigest()

    def netlify_req(method, path, data=None, content_type="application/json"):
        url = f"https://api.netlify.com/api/v1{path}"
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {NETLIFY_TOKEN}",
                     "Content-Type": content_type}
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())

    # Step 1: Create deploy with file manifest
    deploy = netlify_req(
        "POST", f"/sites/{NETLIFY_SITE_ID}/deploys",
        data=json.dumps({"files": {"/index.html": sha1}}).encode(),
    )
    deploy_id = deploy["id"]
    required  = deploy.get("required", [])
    log(f"  Netlify deploy {deploy_id} created (required={required})")

    # Step 2: Upload file if Netlify doesn't have it cached
    if sha1 in required:
        log("  Uploading index.html ‚Ä¶")
        netlify_req(
            "PUT", f"/deploys/{deploy_id}/files/index.html",
            data=html_bytes, content_type="application/octet-stream",
        )

    # Step 3: Wait for ready (up to 60 s)
    for attempt in range(20):
        time.sleep(3)
        status = netlify_req("GET", f"/deploys/{deploy_id}")
        state  = status.get("state")
        if state == "ready":
            live_url = status.get("ssl_url") or NETLIFY_SITE_URL
            log(f"  Netlify deploy ready ‚Üí {live_url}")
            return live_url
        if state in ("error", "failed"):
            raise RuntimeError(f"Netlify deploy {deploy_id} failed: state={state}")
        log(f"  Waiting for Netlify ‚Ä¶ state={state} (attempt {attempt+1}/20)")

    raise RuntimeError(f"Netlify deploy {deploy_id} timed out")


# ‚îÄ‚îÄ Google Sheets ‚Äî metadata only (no Q&A) ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
def _load_sa():
    raw = GOOGLE_SHEETS_KEY.strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def _sa_token():
    sa = _load_sa()
    if not sa:
        return None
    header  = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    now     = int(time.time())
    claim   = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600, "iat": now,
    }
    payload      = base64.urlsafe_b64encode(json.dumps(claim).encode()).rstrip(b"=")
    signing_input = header + b"." + payload
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    pkey = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    sig  = pkey.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt  = signing_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt.decode(),
    }).encode()
    with urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token",
                               data=body, method="POST"), timeout=20
    ) as r:
        return json.loads(r.read())["access_token"]


def _sheets_req(method, path, token, data=None):
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{path}"
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sync_offer_metadata(token, parsed):
    """
    Write offer metadata (NOT Q&A) to the Google Sheet.
    This keeps the sheet's active/inactive + pricing/URL fields current
    so the portal knows which offers to show.
    """
    if not parsed["offer_name"] or not token:
        return None

    values = _sheets_req("GET", f"/values/{urllib.parse.quote(OFFERS_TAB + '!A1:Z2000')}", token)
    rows   = values.get("values", [])
    if not rows:
        return None
    headers = rows[0]
    idx     = {h: i for i, h in enumerate(headers)}

    target = None
    for i, row in enumerate(rows[1:], start=2):
        cn = row[idx.get("client_name", 0)] if len(row) > idx.get("client_name", 0) else ""
        on = row[idx.get("offer_name",  1)] if len(row) > idx.get("offer_name",  1) else ""
        if norm(cn) == norm(parsed["client_name"]) and norm(on) == norm(parsed["offer_name"]):
            target = i
            break

    if target:
        existing = list(rows[target - 1]) + [""] * (len(headers) - len(rows[target - 1]))
        new_row  = existing[:]
    else:
        new_row = [""] * len(headers)

    def setv(col, val):
        if col in idx and val:
            new_row[idx[col]] = val

    setv("client_name",  parsed["client_name"])
    setv("offer_name",   parsed["offer_name"])
    setv("price",        parsed["price"])
    setv("landing_page", parsed["landing_page"])
    setv("booking_page", parsed["booking_page"])
    # Set active=YES for new ADD rows (leave existing active status alone on UPDATE)
    if not target and parsed["action"] == "ADD" and "active" in idx:
        new_row[idx["active"]] = "YES"
    # NOTE: intentionally NOT writing "faq" column ‚Äî Q&A lives in HARDCODED now

    rng = f"{OFFERS_TAB}!A{target}:{chr(ord('A') + len(headers) - 1)}{target}" if target \
          else f"{OFFERS_TAB}!A1"
    method = "PUT" if target else "POST"
    path_suffix = f"/values/{urllib.parse.quote(rng)}?valueInputOption=RAW" if target \
                  else f"/values/{urllib.parse.quote(OFFERS_TAB + '!A1')}:append?valueInputOption=RAW"
    _sheets_req(method, path_suffix, token, json.dumps({"values": [new_row]}).encode())
    return f"{'updated' if target else 'appended'} offers row"


def sync_clinic_metadata(token, parsed):
    """Write clinic-level metadata (deposit, CRM, notes) to the clinics tab."""
    has_data = any(parsed.get(f) for f in
                   ["deposit", "crm_url", "crm_login", "crm_password",
                    "machines", "golden_rule", "ops_notes"])
    if not has_data or not token:
        return None

    values  = _sheets_req("GET", f"/values/{urllib.parse.quote(CLINICS_TAB + '!A1:Z2000')}", token)
    rows    = values.get("values", [])
    if not rows:
        return None
    headers = rows[0]
    idx     = {h: i for i, h in enumerate(headers)}

    target = None
    for i, row in enumerate(rows[1:], start=2):
        cn = row[idx.get("client_name", 0)] if len(row) > idx.get("client_name", 0) else ""
        if norm(cn) == norm(parsed["client_name"]):
            target = i
            break

    if target:
        existing = list(rows[target - 1]) + [""] * (len(headers) - len(rows[target - 1]))
        new_row  = existing[:]
    else:
        new_row = [""] * len(headers)

    def setv(col, val):
        if col in idx and val:
            new_row[idx[col]] = val

    setv("client_name",  parsed["client_name"])
    setv("deposit",      parsed["deposit"])
    setv("crm_url",      parsed["crm_url"])
    setv("crm_login",    parsed["crm_login"])
    setv("crm_password", parsed["crm_password"])
    setv("machines",     parsed["machines"])
    setv("golden_rule",  parsed["golden_rule"])
    setv("ops_notes",    parsed["ops_notes"])
    # NOTE: clinic-level FAQ is also skipped ‚Äî HARDCODED is the source of truth

    rng = f"{CLINICS_TAB}!A{target}:{chr(ord('A') + len(headers) - 1)}{target}" if target \
          else f"{CLINICS_TAB}!A1"
    method = "PUT" if target else "POST"
    path_suffix = f"/values/{urllib.parse.quote(rng)}?valueInputOption=RAW" if target \
                  else f"/values/{urllib.parse.quote(CLINICS_TAB + '!A1')}:append?valueInputOption=RAW"
    _sheets_req(method, path_suffix, token, json.dumps({"values": [new_row]}).encode())
    return f"{'updated' if target else 'appended'} clinics row"


# ‚îÄ‚îÄ Main ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
def main():
    log("=== Script-Updates Sync v2 ===")

    if not SLACK_BOT_TOKEN:
        log("SLACK_BOT_TOKEN missing ‚Äî cannot poll Slack. Exiting.")
        sys.exit(1)
    if not NETLIFY_TOKEN or not NETLIFY_SITE_ID:
        log("NETLIFY_TOKEN or NETLIFY_SITE_ID missing ‚Äî cannot deploy. Exiting.")
        sys.exit(1)

    # ‚îÄ‚îÄ 1. Fetch new Slack messages ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    last_ts = get_last_ts()
    log(f"  Polling since ts={last_ts}")
    msgs = fetch_messages_since(last_ts)
    log(f"  Fetched {len(msgs)} new message(s)")

    # Filter to Sophia's ACTION posts only
    action_posts = []
    latest_ts    = last_ts
    for m in msgs:
        ts   = m.get("ts", "0")
        user = m.get("user", "")
        text = (m.get("text", "") or "").strip()
        latest_ts = ts
        # Process posts from Sophia AND Mai (Jeamai) — both maintain script updates.
        # Bug fixed 2026-05-13: previously only Sophia was processed, Mai's updates
        # were silently dropped (e.g., LaVie price changes, Modern Image deposit).
        if user in ALLOWED_USER_IDS and text.upper().startswith("ACTION"):
            parsed = parse_action_post(text)
            if parsed and parsed["client_name"]:
                action_posts.append(parsed)
                log(f"  Queued: {parsed['action']} {parsed['client_name']}"
                    f"{' / ' + parsed['offer_name'] if parsed['offer_name'] else ''}"
                    f"  qanda={len(faq_to_qanda(parsed.get('faq_raw', '')))}")
            else:
                log(f"  Skipped (parse failed): ts={ts}")

    if not action_posts:
        log("Nothing to do.")
        set_last_ts(latest_ts)
        return

    # ‚îÄ‚îÄ 2. Load current index.html and extract HARDCODED ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    log("  Fetching index.html from Netlify ‚Ä¶")
    html    = fetch_portal_html()
    hc_data, hc_match = extract_hardcoded(html)
    log(f"  Loaded HARDCODED with {len(hc_data)} clinics")

    # ‚îÄ‚îÄ 3. Apply all changes to HARDCODED in memory ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    hardcoded_changes = []
    for p in action_posts:
        desc = apply_to_hardcoded(hc_data, p)
        hardcoded_changes.append(desc)
        log(f"  HARDCODED: {desc}")

    # ‚îÄ‚îÄ 4. Rebuild index.html with updated HARDCODED ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    new_html = replace_hardcoded(html, hc_match, hc_data)

    # ‚îÄ‚îÄ 5. Deploy to Netlify ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    log("  Deploying to Netlify ‚Ä¶")
    try:
        live_url = netlify_deploy(new_html)
    except Exception as e:
        log(f"  NETLIFY DEPLOY FAILED: {e}")
        slack_dm(SOPHIA_USER_ID,
                 f"‚ö†Ô∏è Script update received but *Netlify deploy failed*: `{e}`\n"
                 f"Changes will be retried on the next sync run.")
        sys.exit(1)

    # ‚îÄ‚îÄ 6. Update Google Sheet metadata (non-FAQ fields only) ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    sheet_token = None
    if GOOGLE_SHEETS_KEY:
        try:
            sheet_token = _sa_token()
        except Exception as e:
            log(f"  Warning: Could not get Sheets token: {e} (metadata update skipped)")

    if sheet_token:
        for p in action_posts:
            try:
                r1 = sync_offer_metadata(sheet_token, p)
                r2 = sync_clinic_metadata(sheet_token, p)
                log(f"  Sheet: offers={r1}  clinics={r2}")
            except Exception as e:
                log(f"  Sheet metadata write failed (non-fatal): {e}")
            time.sleep(0.4)

    # ‚îÄ‚îÄ 7. Save state and notify Sophia ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ‚îÄ
    set_last_ts(latest_ts)

    n = len(action_posts)
    summary = "\n".join(f"  ‚Ä¢ {d}" for d in hardcoded_changes)
    slack_dm(
        SOPHIA_USER_ID,
        f"‚úÖ *{n} script update{'s' if n > 1 else ''} applied and live* at "
        f"<{live_url}|agacallcenter.netlify.app>\n{summary}"
    )
    log(f"Done. Applied {n} update(s), deployed to Netlify.")


if __name__ == "__main__":
    main()
