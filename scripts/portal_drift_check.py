#!/usr/bin/env python3
"""
AGA Portal Drift Check — runs daily via GitHub Actions.

Detects gaps between the live Google Sheet (the source of truth Sophia maintains)
and the HARDCODED baseline data baked into agacallcenter.netlify.app's HTML.

Why this matters: the portal merges HARDCODED + Sheet at runtime. If the Sheet
fetch fails (5-minute outage, sync error), reps see stale HARDCODED data. If the
HARDCODED is months out of date, reps may be quoting wrong prices, missing offers,
or seeing dead clinics during a partial outage.

What this script does:
  1. Fetch the live offers tab from Google Sheets
  2. Fetch the live clinics tab from Google Sheets
  3. Fetch the live portal HTML and extract the HARDCODED baseline
  4. Diff them — surface clinics/offers in Sheet but missing from baseline
                 (and vice versa)
  5. DM Juan if drift exceeds a threshold (e.g., 5+ missing clinics)

This is read-only. It doesn't fix anything — just alerts when the baseline
needs a refresh.
"""
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import ALERT_WEBHOOK
from _schedule import EST

SHEET_ID = "1wZEQYV4RgjbWHrRhnw58DIiFGNrOM5-jlJQuNv3_nFw"
PORTAL_URL = "https://agacallcenter.netlify.app/"

# Thresholds — alert if drift exceeds these
MISSING_CLINIC_THRESHOLD = 5   # 5+ clinics in Sheet but not HARDCODED
PRICE_MISMATCH_THRESHOLD = 5   # 5+ offers with price mismatch


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fetch_text(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def parse_csv(text):
    """Tiny CSV parser handling quoted fields."""
    import csv, io
    return list(csv.DictReader(io.StringIO(text)))


def normalize_name(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def fetch_sheet():
    offers_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"
    clinics_url = f"{offers_url}&sheet=clinics"
    offers = parse_csv(fetch_text(offers_url))
    clinics = parse_csv(fetch_text(clinics_url))
    return offers, clinics


def fetch_hardcoded():
    """Pull HARDCODED data from the live portal HTML."""
    html = fetch_text(PORTAL_URL)
    m = re.search(r"const HARDCODED\s*=\s*(\[.*?\]);", html, re.DOTALL)
    if not m:
        raise RuntimeError("Could not find HARDCODED in portal HTML")
    return json.loads(m.group(1))


def alert(title, body):
    payload = {
        "rep": "_SYSTEM",
        "pattern": "PORTAL_DRIFT",
        "title": title,
        "message": body,
        "stats": {"dials": 0, "connects": 0},
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    urllib.request.urlopen(
        urllib.request.Request(
            ALERT_WEBHOOK, method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        ), timeout=15
    ).read()


def main():
    log("=== Portal Drift Check ===")
    offers, clinics = fetch_sheet()
    hc = fetch_hardcoded()

    sheet_clinics = {normalize_name(o.get("client_name", "")) for o in offers if o.get("client_name")}
    sheet_clinics |= {normalize_name(c.get("client_name", "")) for c in clinics if c.get("client_name")}
    hc_clinics = {normalize_name(c.get("name", "")) for c in hc}

    in_sheet_not_hc = sorted(sheet_clinics - hc_clinics - {""})
    in_hc_not_sheet = sorted(hc_clinics - sheet_clinics - {""})

    log(f"Sheet clinics: {len(sheet_clinics)}")
    log(f"HARDCODED clinics: {len(hc_clinics)}")
    log(f"In Sheet but not HARDCODED: {len(in_sheet_not_hc)}")
    log(f"In HARDCODED but not Sheet: {len(in_hc_not_sheet)}")

    if in_sheet_not_hc:
        log("  Missing from HARDCODED:")
        for c in in_sheet_not_hc[:10]:
            log(f"    - {c}")

    # Price-mismatch detection: same clinic+offer in both, different prices
    hc_offer_prices = {}
    for c in hc:
        for o in c.get("offers", []) or []:
            key = (normalize_name(c.get("name", "")), normalize_name(o.get("name", "")))
            hc_offer_prices[key] = (o.get("price") or "").strip()
    price_mismatches = []
    for o in offers:
        key = (normalize_name(o.get("client_name", "")), normalize_name(o.get("offer_name", "")))
        if key in hc_offer_prices:
            sheet_price = (o.get("price") or "").strip()
            hc_price = hc_offer_prices[key]
            if sheet_price and hc_price and sheet_price != hc_price:
                price_mismatches.append((key, hc_price, sheet_price))
    log(f"Price mismatches: {len(price_mismatches)}")
    for (cl, off), hcp, sp in price_mismatches[:10]:
        log(f"    {cl} / {off}: HARDCODED={hcp} Sheet={sp}")

    # Alert if drift exceeds thresholds
    needs_alert = (
        len(in_sheet_not_hc) >= MISSING_CLINIC_THRESHOLD
        or len(price_mismatches) >= PRICE_MISMATCH_THRESHOLD
    )
    if needs_alert:
        body_lines = [
            f"*Portal HARDCODED baseline is drifting from the Google Sheet.*",
            f"• {len(in_sheet_not_hc)} clinic(s) in Sheet but missing from HARDCODED.",
            f"• {len(price_mismatches)} offer(s) with price mismatches.",
            "",
            "If the Sheet sync ever fails, reps will see stale HARDCODED data. "
            "Time to refresh the portal's baked-in baseline.",
        ]
        if in_sheet_not_hc:
            body_lines.append("\n_Missing clinics:_")
            body_lines.extend(f"  • {c}" for c in in_sheet_not_hc[:8])
        if price_mismatches:
            body_lines.append("\n_Price mismatches:_")
            for (cl, off), hcp, sp in price_mismatches[:5]:
                body_lines.append(f"  • {cl} → {off}: HARDCODED `{hcp}` vs Sheet `{sp}`")
        alert(
            f"⚠️ Portal HARDCODED baseline drifting ({len(in_sheet_not_hc)} clinics, "
            f"{len(price_mismatches)} prices)",
            "\n".join(body_lines)
        )
        log("→ Alert fired.")
    else:
        log("→ No alert (drift within thresholds).")

    log("Done.")


if __name__ == "__main__":
    main()
