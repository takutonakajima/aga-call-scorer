#!/usr/bin/env python3
"""
AGA Daily Data Backup — runs daily via GitHub Actions, commits to repo.

Snapshots each Make.com data store (call scores, bookings, daily dials, weekly
tips, top call, shift overrides) to JSON files under `backups/YYYY-MM-DD/`.
Workflow then commits back to the repo. 90 days of rolling history.

If a data store ever gets accidentally wiped or corrupted, you can restore
from the most recent JSON snapshot — no human-detectable data loss.
"""
import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config import SCORE_API
from _schedule import EST

BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"

# All read APIs we want to snapshot (name → URL)
APIS = {
    "call_scores":      SCORE_API,
    "bookings":         "https://hook.us2.make.com/zta4icvee2vs637c7h823dbbvl3dma3g",
    "daily_dials":      "https://hook.us2.make.com/iqiuk23ks9uscx79rs3b5pqj85tnugqi",
    "weekly_tips":      "https://hook.us2.make.com/92wixa36ff24n1k6dt57a4mn4qrfxylb",
    "top_calls":        "https://hook.us2.make.com/2ncatyrspbm62lt98vah5ef362sf89wv",
    "shift_overrides":  "https://hook.us2.make.com/f1linqm93gfo53xrigduqa0498usvnb6",
    "anomaly_state":    os.environ.get("ANOMALY_STATE_API_URL", ""),
}

RETENTION_DAYS = 90  # delete snapshot folders older than this


def log(msg):
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(
        url, method="POST", data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main():
    today = datetime.now(EST).strftime("%Y-%m-%d")
    out = BACKUP_DIR / today
    out.mkdir(parents=True, exist_ok=True)
    log(f"=== Backing up to {out} ===")

    summary = {}
    for name, url in APIS.items():
        if not url:
            log(f"  {name}: skipped (no URL)")
            continue
        try:
            data = fetch(url)
            count = len(data) if isinstance(data, list) else 1
            (out / f"{name}.json").write_text(json.dumps(data, indent=2))
            summary[name] = count
            log(f"  ✅ {name}: {count} records")
        except Exception as e:
            log(f"  ❌ {name}: FAIL {type(e).__name__}: {e}")
            summary[name] = f"ERROR: {e}"

    # Write a small summary index
    (out / "_summary.json").write_text(json.dumps({
        "snapshot_date": today,
        "snapshot_at_utc": datetime.now(timezone.utc).isoformat(),
        "counts": summary,
    }, indent=2))

    # Retention: delete snapshot dirs older than RETENTION_DAYS
    if BACKUP_DIR.exists():
        cutoff = (datetime.now(EST) - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
        for d in sorted(BACKUP_DIR.iterdir()):
            if d.is_dir() and d.name < cutoff:
                log(f"  Removing old snapshot: {d.name}")
                for f in d.iterdir():
                    f.unlink()
                d.rmdir()

    log("Done.")


if __name__ == "__main__":
    main()
