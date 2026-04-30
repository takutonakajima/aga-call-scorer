"""
Rep shift schedule — 4-week rotating cycle with 4 shift blocks.

Reference week (Mon Apr 27, 2026):
  Block A (8am – 2pm ET)   →  Jelyn
  Block B (9am – 3pm ET)   →  Sarah
  Block C (2pm – 8pm ET)   →  Chris
  Block D (4pm – 10pm ET)  →  Julie

Each rep advances one block per week (A → B → C → D → A → ...).
On Sundays, only the rep on Block B is scheduled.
"""
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

ROTATION = ["A", "B", "C", "D"]

BLOCKS = {
    "A": {"label": "8am – 2pm",  "start_h": 8,  "end_h": 14},
    "B": {"label": "9am – 3pm",  "start_h": 9,  "end_h": 15},
    "C": {"label": "2pm – 8pm",  "start_h": 14, "end_h": 20},
    "D": {"label": "4pm – 10pm", "start_h": 16, "end_h": 22},
}

# The week starting this Monday is the reference. All other weeks computed by rotation.
REFERENCE_WEEK = date(2026, 4, 27)
REFERENCE_ASSIGNMENTS = {
    "Jelyn": "A",
    "Sarah": "B",
    "Chris": "C",
    "Julie": "D",
}


def _week_monday(target_date):
    return target_date - timedelta(days=target_date.weekday())


def block_for_rep(rep, target_date):
    """Which shift block is the rep on, for the week containing target_date?"""
    if rep not in REFERENCE_ASSIGNMENTS:
        return None
    monday = _week_monday(target_date)
    weeks_diff = (monday - REFERENCE_WEEK).days // 7
    base_idx = ROTATION.index(REFERENCE_ASSIGNMENTS[rep])
    new_idx = (base_idx + weeks_diff) % len(ROTATION)
    return ROTATION[new_idx]


def shift_label_for_rep(rep, target_date=None):
    """e.g. '9am – 3pm' for the rep's current week."""
    if target_date is None:
        target_date = datetime.now(EST).date()
    block = block_for_rep(rep, target_date)
    return BLOCKS[block]["label"] if block else None


def shift_status(rep, dt_est=None):
    """Return one of: 'on_shift', 'before_shift', 'after_shift', 'off_day'.

    Sundays: only the Block-B rep this week is on a working day.
    """
    if dt_est is None:
        dt_est = datetime.now(EST)
    block = block_for_rep(rep, dt_est.date())
    if not block:
        return "off_day"
    # Sunday — only Block-B rep works
    if dt_est.weekday() == 6 and block != "B":
        return "off_day"
    b = BLOCKS[block]
    if dt_est.hour < b["start_h"]:
        return "before_shift"
    if dt_est.hour >= b["end_h"]:
        return "after_shift"
    return "on_shift"


def is_on_shift(rep, dt_est=None):
    return shift_status(rep, dt_est) == "on_shift"


def all_assignments_for_week(target_date=None):
    """Map of {rep: block_id} for the week containing target_date."""
    if target_date is None:
        target_date = datetime.now(EST).date()
    return {rep: block_for_rep(rep, target_date) for rep in REFERENCE_ASSIGNMENTS}
