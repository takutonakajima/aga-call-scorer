#!/usr/bin/env python3
"""
AGA #script-updates → Google Sheet sync.

Polls Slack channel C0AV8UBGC69 (#script-updates) every 5 min via GitHub Actions.
Parses Sophia's structured ACTION:ADD / ACTION:UPDATE posts and writes them into
the master call-center Google Sheet (offers + clinics tabs) so the portal at
agacallcenter.netlify.app picks them up automatically.

Why this exists:
  Sophia posts script updates in Slack. Until now somebody (Juan) had to copy
  every field into the sheet manually. Sophia's FAQs were getting truncated
  because they sit in a Slack message and never make it into the Sheet `faq`
  column. This closes that loop.

Auth (set as GitHub Actions secrets):
  SLACK_BOT_TOKEN   — bot token for agahqworkspace, scopes: channels:history,
                      chat:write, users:read. Bot must be a member of
                      #script-updates.
  GOOGLE_SHEETS_KEY — Google service-account JSON (single-line, base64 encoded
                      OR raw JSON). Service account email must have Editor
                      access to sheet 1wZEQYV4RgjbWHrRhnw58DIiFGNrOM5-jlJQuNv3_nFw.
  SCRIPT_SYNC_STATE_GET / SCRIPT_SYNC_STATE_SET — Make.com data store
                      webhooks for tracking last-processed message ts.

Format Sophia uses (from #aga-resolve template, May 1):
  ACTION: [ADD / UPDATE]
  CLINIC: [Clinic Name]
  OFFER NAME: [Offer Name]
  PRICE: [$XXX]
  LANDING PAGE: [url or blank]
  BOOKING PAGE: [url or blank]
  DEPOSIT: [$XX]
  CRM LINK: [url]
  CRM LOGIN: [email]
  CRM PASSWORD: [password]
  NOTES: [text]
  FAQ:
  Q: [question]
  A: [answer]
  Q: [question 2]
  A: [answer 2]

Output FAQ format expected by the portal:
  Q: question | A: answer || Q: question 2 | A: answer 2
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Add scripts dir to path then import shared config
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- Config ---------------------------------------------------------------
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
GOOGLE_SHEETS_KEY = os.environ.get("GOOGLE_SHEETS_KEY", "")
SCRIPT_SYNC_STATE_GET = os.environ.get("SCRIPT_SYNC_STATE_GET", "")
SCRIPT_SYNC_STATE_SET = os.environ.get("SCRIPT_SYNC_STATE_SET", "")
ALERT_WEBHOOK = os.environ.get("ALERT_WEBHOOK_URL", "")

CHANNEL_ID = "C0AV8UBGC69"           # #script-updates
SHEET_ID = "1wZEQYV4RgjbWHrRhnw58DIiFGNrOM5-jlJQuNv3_nFw"
OFFERS_TAB = "offers"
CLINICS_TAB = "clinics"
SOPHIA_USER_ID = "U0AUCP53T3R"
JUAN_USER_ID = "U0AUMQV7B6X"

# Columns (must match the sheet header row exactly)
OFFERS_HEADERS = ["client_name", "offer_name", "price", "landing_page",
                  "booking_page", "active", "faq"]
CLINICS_HEADERS = ["client_name", "deposit", "crm_url", "crm_login",
                   "crm_password", "machines", "golden_rule", "ops_notes", "faq"]


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


# ---- Slack helpers --------------------------------------------------------
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
        raise RuntimeError(f"slack {method} failed: {body}")
    return body


def fetch_messages_since(oldest_ts):
    """Return Slack messages newer than oldest_ts, oldest-first."""
    body = slack_call("conversations.history", {
        "channel": CHANNEL_ID,
        "oldest": oldest_ts,
        "limit": 200,
    })
    msgs = body.get("messages", [])
    # Slack returns newest first → reverse for chronological processing
    return list(reversed(msgs))


def slack_dm(user_id, text):
    if not SLACK_BOT_TOKEN:
        return
    try:
        slack_call("chat.postMessage",
                   {"channel": user_id, "text": text, "mrkdwn": True},
                   post=True)
    except Exception as e:
        log(f"  Slack DM failed: {e}")


# ---- State (last processed ts) -------------------------------------------
def get_last_ts():
    if not SCRIPT_SYNC_STATE_GET:
        # Fallback: poll last 30 minutes only
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
        log(f"  state set failed: {e}")


# ---- Parser ---------------------------------------------------------------
FIELD_RE = re.compile(
    r"^(ACTION|CLINIC|OFFER NAME|PRICE|LANDING PAGE|BOOKING PAGE|DEPOSIT|"
    r"CRM LINK|CRM LOGIN|CRM PASSWORD|NOTES|MACHINES|GOLDEN RULE|FAQ)\s*:",
    re.IGNORECASE | re.MULTILINE,
)


def strip_brackets(v):
    """Sophia wraps values in [brackets]. Strip them along with markdown junk."""
    if v is None:
        return ""
    v = v.strip()
    # Strip surrounding brackets if balanced
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1].strip()
    # Slack wraps URLs as <url> or <url|text>
    v = re.sub(r"<(https?://[^|>]+)(?:\|[^>]+)?>", r"\1", v)
    v = re.sub(r"<mailto:([^|>]+)(?:\|[^>]+)?>", r"\1", v)
    # Remove leading/trailing markdown asterisks
    v = v.strip().strip("*").strip()
    return v


def faq_to_single_cell(faq_block):
    """
    Convert Sophia's multi-line `Q: ...\nA: ...` block into the portal's
    single-cell format `Q: q | A: a || Q: q2 | A: a2`.
    """
    if not faq_block.strip():
        return ""
    pairs = []
    cur_q, cur_a = None, []
    for raw_line in faq_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip stray markdown `*` and `[`/`]` wrapping
        line = re.sub(r"^\*+|\*+$", "", line).strip()
        m_q = re.match(r"^Q\s*:\s*\[?(.*?)\]?\*?\s*$", line, re.IGNORECASE)
        m_a = re.match(r"^A\s*:\s*\[?(.*?)\]?\*?\s*$", line, re.IGNORECASE)
        if m_q:
            if cur_q and cur_a:
                pairs.append((cur_q, " ".join(cur_a).strip()))
            cur_q = strip_brackets(m_q.group(1)).strip().rstrip("?") + "?"
            cur_a = []
        elif m_a:
            cur_a.append(strip_brackets(m_a.group(1)).strip())
        elif cur_a is not None and cur_q is not None:
            # continuation of previous answer
            cur_a.append(strip_brackets(line))
    if cur_q and cur_a:
        pairs.append((cur_q, " ".join(cur_a).strip()))
    parts = []
    for q, a in pairs:
        # Escape any literal '|' in the answer so it doesn't collide with our delimiters
        a_safe = a.replace("||", "/").replace(" | ", " — ")
        parts.append(f"Q: {q} | A: {a_safe}")
    return " || ".join(parts)


def parse_action_post(text):
    """Parse a single ACTION:ADD/UPDATE post into a dict."""
    if not text or not text.lstrip().upper().startswith("ACTION"):
        return None
    fields = {}
    # Find every field header and capture the text up to the next header
    matches = list(FIELD_RE.finditer(text))
    for i, m in enumerate(matches):
        name = m.group(1).upper().replace(" ", "_")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        fields[name] = value

    if "ACTION" not in fields:
        return None

    # Normalize values
    out = {
        "action": strip_brackets(fields.get("ACTION", "")).upper(),
        "client_name": strip_brackets(fields.get("CLINIC", "")),
        "offer_name": strip_brackets(fields.get("OFFER_NAME", "")),
        "price": strip_brackets(fields.get("PRICE", "")),
        "landing_page": strip_brackets(fields.get("LANDING_PAGE", "")),
        "booking_page": strip_brackets(fields.get("BOOKING_PAGE", "")),
        "deposit": strip_brackets(fields.get("DEPOSIT", "")),
        "crm_url": strip_brackets(fields.get("CRM_LINK", "")),
        "crm_login": strip_brackets(fields.get("CRM_LOGIN", "")),
        "crm_password": strip_brackets(fields.get("CRM_PASSWORD", "")),
        "machines": strip_brackets(fields.get("MACHINES", "")),
        "golden_rule": strip_brackets(fields.get("GOLDEN_RULE", "")),
        "ops_notes": strip_brackets(fields.get("NOTES", "")),
        "faq": faq_to_single_cell(fields.get("FAQ", "")),
    }
    if not out["client_name"]:
        return None
    return out


# ---- Google Sheets (service account REST) --------------------------------
def _load_sa():
    raw = GOOGLE_SHEETS_KEY.strip()
    if not raw:
        raise RuntimeError("GOOGLE_SHEETS_KEY env var is empty")
    if not raw.startswith("{"):
        # Treat as base64
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def _sa_token():
    """Mint a short-lived Google access token from the service-account key."""
    import hashlib, hmac
    sa = _load_sa()
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "RS256", "typ": "JWT"}).encode()).rstrip(b"=")
    now = int(time.time())
    claim = {
        "iss": sa["client_email"],
        "scope": "https://www.googleapis.com/auth/spreadsheets",
        "aud": "https://oauth2.googleapis.com/token",
        "exp": now + 3600,
        "iat": now,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claim).encode()).rstrip(b"=")
    signing_input = header + b"." + payload

    # RS256 signing — requires `cryptography` package
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    pkey = serialization.load_pem_private_key(
        sa["private_key"].encode(), password=None)
    sig = pkey.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    jwt = signing_input + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")

    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt.decode(),
    }).encode()
    with urllib.request.urlopen(
        urllib.request.Request("https://oauth2.googleapis.com/token",
                               data=body, method="POST"),
        timeout=20,
    ) as r:
        return json.loads(r.read())["access_token"]


def sheets_get_values(token, tab):
    rng = f"{tab}!A1:Z2000"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{urllib.parse.quote(rng)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read()).get("values", [])


def sheets_update_row(token, tab, row_index_1based, headers, row_values):
    """row_values is a list aligned to headers. row_index_1based is sheet row #."""
    rng = f"{tab}!A{row_index_1based}:{chr(ord('A') + len(headers) - 1)}{row_index_1based}"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           f"{urllib.parse.quote(rng)}?valueInputOption=RAW")
    body = json.dumps({"values": [row_values]}).encode()
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def sheets_append_row(token, tab, headers, row_values):
    rng = f"{tab}!A1"
    url = (f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/"
           f"{urllib.parse.quote(rng)}:append?valueInputOption=RAW")
    body = json.dumps({"values": [row_values]}).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ---- Apply parsed post to the sheet --------------------------------------
def norm(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def apply_to_offers(token, parsed):
    """Write the offer-level row (client_name, offer_name, price, urls, faq)."""
    if not parsed["offer_name"]:
        return None  # nothing to write to offers tab without an offer
    values = sheets_get_values(token, OFFERS_TAB)
    if not values:
        raise RuntimeError("offers tab is empty?")
    headers = values[0]
    idx = {h: i for i, h in enumerate(headers)}

    # Find existing row by (client_name, offer_name) - case-insensitive
    target = None
    for i, row in enumerate(values[1:], start=2):  # row 2 = data row 1
        cn = row[idx.get("client_name", 0)] if len(row) > idx.get("client_name", 0) else ""
        on = row[idx.get("offer_name", 1)] if len(row) > idx.get("offer_name", 1) else ""
        if norm(cn) == norm(parsed["client_name"]) and norm(on) == norm(parsed["offer_name"]):
            target = i
            break

    # Build row aligned to existing headers; preserve unset fields on UPDATE
    if target:
        existing = values[target - 1] if len(values) >= target else []
        existing = list(existing) + [""] * (len(headers) - len(existing))
        new_row = existing[:]
    else:
        new_row = [""] * len(headers)

    def setv(col, val):
        if col in idx and val:
            new_row[idx[col]] = val

    setv("client_name", parsed["client_name"])
    setv("offer_name", parsed["offer_name"])
    setv("price", parsed["price"])
    setv("landing_page", parsed["landing_page"])
    setv("booking_page", parsed["booking_page"])
    setv("faq", parsed["faq"])
    if parsed["action"] == "ADD" and "active" in idx and not new_row[idx["active"]]:
        new_row[idx["active"]] = "YES"

    if target:
        sheets_update_row(token, OFFERS_TAB, target, headers, new_row)
        return f"updated offers row {target}"
    else:
        sheets_append_row(token, OFFERS_TAB, headers, new_row)
        return "appended new offers row"


def apply_to_clinics(token, parsed):
    """Write/update the clinic-level row (notes, CRM, deposit, faq)."""
    has_clinic_data = any([parsed["deposit"], parsed["crm_url"],
                           parsed["crm_login"], parsed["crm_password"],
                           parsed["machines"], parsed["golden_rule"],
                           parsed["ops_notes"], parsed["faq"]])
    if not has_clinic_data:
        return None
    values = sheets_get_values(token, CLINICS_TAB)
    if not values:
        return "clinics tab empty (skipped)"
    headers = values[0]
    idx = {h: i for i, h in enumerate(headers)}

    target = None
    for i, row in enumerate(values[1:], start=2):
        cn = row[idx.get("client_name", 0)] if len(row) > idx.get("client_name", 0) else ""
        if norm(cn) == norm(parsed["client_name"]):
            target = i
            break

    if target:
        existing = list(values[target - 1])
        existing += [""] * (len(headers) - len(existing))
        new_row = existing[:]
    else:
        new_row = [""] * len(headers)

    def setv(col, val):
        if col in idx and val:
            new_row[idx[col]] = val

    setv("client_name", parsed["client_name"])
    setv("deposit", parsed["deposit"])
    setv("crm_url", parsed["crm_url"])
    setv("crm_login", parsed["crm_login"])
    setv("crm_password", parsed["crm_password"])
    setv("machines", parsed["machines"])
    setv("golden_rule", parsed["golden_rule"])
    setv("ops_notes", parsed["ops_notes"])
    # Only set clinic-level faq if no offer-specific faq was written
    if parsed["faq"] and not parsed["offer_name"]:
        setv("faq", parsed["faq"])

    if target:
        sheets_update_row(token, CLINICS_TAB, target, headers, new_row)
        return f"updated clinics row {target}"
    else:
        sheets_append_row(token, CLINICS_TAB, headers, new_row)
        return "appended new clinics row"


# ---- Main loop ------------------------------------------------------------
def main():
    log("=== Script-Updates Sync ===")
    if not SLACK_BOT_TOKEN:
        log("SLACK_BOT_TOKEN missing — cannot poll Slack. Exiting.")
        return
    if not GOOGLE_SHEETS_KEY:
        log("GOOGLE_SHEETS_KEY missing — cannot write Sheet. Exiting.")
        return

    last_ts = get_last_ts()
    log(f"  last_ts={last_ts}")

    msgs = fetch_messages_since(last_ts)
    log(f"  fetched {len(msgs)} new messages")

    if not msgs:
        log("Nothing to do.")
        return

    token = _sa_token()
    processed_ts = last_ts
    written = 0
    skipped = 0

    for m in msgs:
        ts = m.get("ts", "0")
        text = m.get("text", "") or ""
        user = m.get("user", "")

        # Only process Sophia's structured ACTION posts (ignore Juan, bots, replies)
        if user != SOPHIA_USER_ID:
            processed_ts = ts
            continue
        if not text.lstrip().upper().startswith("ACTION"):
            processed_ts = ts
            continue

        parsed = parse_action_post(text)
        if not parsed:
            log(f"  ts={ts}: parse failed, skipping")
            skipped += 1
            processed_ts = ts
            continue

        log(f"  ts={ts}: {parsed['action']} {parsed['client_name']} / {parsed['offer_name']}")
        try:
            offer_result = apply_to_offers(token, parsed)
            clinic_result = apply_to_clinics(token, parsed)
            log(f"    offers: {offer_result}    clinics: {clinic_result}")
            written += 1
        except Exception as e:
            log(f"    ERROR: {type(e).__name__}: {e}")
            skipped += 1

        processed_ts = ts
        time.sleep(0.5)  # be gentle to Sheets API

    set_last_ts(processed_ts)

    if written:
        slack_dm(SOPHIA_USER_ID,
                 f"✅ Synced {written} script update(s) into the call-center sheet. "
                 f"They'll show on the portal in ≤5 min (or click ↻ Sync to force).")
    log(f"Done. written={written} skipped={skipped} new_last_ts={processed_ts}")


if __name__ == "__main__":
    main()
