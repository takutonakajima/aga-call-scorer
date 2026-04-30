# AGA Call Coaching — Cron Scripts

Automated background jobs that power the [Call Coaching Dashboard](https://github.com/your-username/call-coaching-ai).

## What runs here

| Script | Frequency | Purpose |
|--------|-----------|---------|
| `score_calls.py` | every 15 min | Pulls new Twilio recordings, scores via Gemini, writes to Make.com |
| `dial_tracker.py` | every 30 min | Counts daily dials per rep for the 200/day quota |
| `anomaly_detector.py` | hourly | Detects voicemail walls / low-connect-rate / silent-shift patterns and DMs Sophia + Juan |
| `weekly_digest.py` | Mondays 8am ET | Generates 3 personalized coaching modules per rep |
| `top_call.py` | Mondays 8:05am ET | Picks the team's best call, downloads MP3, generates teardown |

## How GitHub Actions runs them

Each workflow in `.github/workflows/` defines a cron schedule and runs the matching Python script on Ubuntu runners. Credentials come from GitHub Secrets (encrypted, never visible in logs).

## Setup (one-time)

1. **Push this repo** to GitHub (public repo for unlimited free Actions minutes).
2. **Add the 9 secrets** under Settings → Secrets and variables → Actions:
   - `TWILIO_ACCOUNT`
   - `TWILIO_AUTH`
   - `GEMINI_KEY`
   - `SCORE_INGEST_URL`
   - `SCORE_API_URL`
   - `DIALS_INGEST_URL`
   - `TIPS_INGEST_URL`
   - `TOPCALL_INGEST_URL`
   - `ALERT_WEBHOOK_URL`

   *(actual values are kept private — request them from the project owner)*

3. **Disable the local launchd jobs** (one-time, on the Mac that was running them):
   ```sh
   launchctl unload ~/Library/LaunchAgents/com.aga.callscorer.plist
   launchctl unload ~/Library/LaunchAgents/com.aga.dialtracker.plist
   launchctl unload ~/Library/LaunchAgents/com.aga.anomalydetector.plist
   launchctl unload ~/Library/LaunchAgents/com.aga.weeklydigest.plist
   launchctl unload ~/Library/LaunchAgents/com.aga.topcall.plist
   ```

4. **Trigger one workflow manually** (Actions tab → pick a workflow → Run workflow). If it succeeds, you're live.

## Local dev

Set env vars in your shell, then:
```sh
python scripts/score_calls.py
```

## State

Stateless by design. Each run derives "what's already done" from the live Make.com data stores via the Score API. Anomaly dedup is best-effort — patterns may re-alert hourly while persisting (mute the Slack DM if noisy).

## Cost

Public repo → unlimited free Actions minutes. ~$0.50/month in Gemini API spend at current scoring volume.

## Owners

- Juan Arango (CEO)
- Takuto Nakajima (technical co-founder)
