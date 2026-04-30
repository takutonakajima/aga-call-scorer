"""
Shared config — credentials loaded from environment variables.
In GitHub Actions these come from Secrets. Locally, set them in your shell.
"""
import os

TWILIO_ACCOUNT = os.environ["TWILIO_ACCOUNT"]
TWILIO_AUTH = os.environ["TWILIO_AUTH"]               # full "Basic XXX..." header value
GEMINI_KEY = os.environ["GEMINI_KEY"]

# Make.com webhook URLs
SCORE_INGEST = os.environ["SCORE_INGEST_URL"]         # call scorer write
SCORE_API = os.environ["SCORE_API_URL"]               # call scorer read
DIALS_INGEST = os.environ["DIALS_INGEST_URL"]
TIPS_INGEST = os.environ["TIPS_INGEST_URL"]
TOPCALL_INGEST = os.environ["TOPCALL_INGEST_URL"]
ALERT_WEBHOOK = os.environ["ALERT_WEBHOOK_URL"]

REP_MAP = {
    "117-243": "Chris",
    "117-153": "Jelyn",
    "117-151": "Sarah",
    "117-242": "Julie",
}
