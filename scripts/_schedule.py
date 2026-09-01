"""
Rep shift schedule — era-aware rotation.

⚠️  THIS FILE IS THE SOURCE OF TRUTH FOR THE SHIFT SCHEDULE.

⚠️  IF YOU CHANGE BLOCKS, ROTATION, REFERENCE WEEK, OR ASSIGNMENTS BELOW,
    YOU MUST ALSO UPDATE THESE MIRRORS (constants near the top of each):
      • Call Coaching AI/rep.html      → SHIFT_* constants
      • Call Coaching AI/manager.html  → SHIFT_* constants
      • spaboost-intelligence/supabase/functions/lead-pipeline-sync/index.ts
        → SHIFT_BLOCKS / rotation constants (auto-assignment on-shift check)
      • spaboost-intelligence/supabase/functions/shift-assigner/index.ts
        → SHIFT_UNITS / UNIT_ROTATION (unitRotationBlock)
      • spaboost-intelligence/supabase/functions/resolve-bridge/index.ts
        → SHIFT_UNITS / UNIT_ROTATION (blockFor)

ERA 1 (legacy, weeks before Mon 2026-08-17): 4 setters, blocks A–D,
  weekly rotation A→B→C→D. Sundays: only Block-B rep works.

ERA 2 (from Mon 2026-08-17): 7 setters — 4 veterans + 3 trainees
  (Juan 2026-08-12: "2-3 people online at all times, power dialing and
  speed to lead"). Two parallel rotations over 3 paired blocks + a
  veteran-only Flex block:

    Block E (8am – 2pm ET)   veteran + trainee
    Block M (11am – 5pm ET)  veteran + trainee   ← Sunday crew
    Block L (4pm – 10pm ET)  veteran + trainee
    Block F (12pm – 6pm ET)  4th veteran, peak reinforcement

  Coverage: ≥2 online 8am–10pm, 3–5 during 11am–6pm peak.
  Within a shift, ONE person at a time is "Speed-to-Lead point" (fresh
  leads + red board cards, <5 min response); everyone else power-dials.
  Point rotates at the top of each hour.

  Veterans rotate E→M→L→F weekly; trainees rotate E→M→L weekly, so every
  trainee pairs with a different veteran each week.
  Sundays: only the two M-block reps work.

ERA 3 (pairs/units, weeks from Mon 2026-08-24): the 7 setters are grouped
  into 4 FIXED UNITS that rotate together through the blocks in
  chronological order E → M → F → L (⚠️ order differs from era-2's veteran
  rotation E → M → L → F). Anchored to the grid Sophia set by hand for the
  week of Mon 2026-08-24:

    Jane + Julie       2026-08-24: E
    Jamaica + Jelyn    2026-08-24: M
    Sarah + Debbie     2026-08-24: F
    Christelle (solo)  2026-08-24: L

  The gate is WEEK-level (unlike era-2's mid-week go-live): a week is
  pairs-era iff its Monday is on/after 2026-08-24. shift_overrides rows
  still beat the rotation. Sundays: the whole M unit works.
"""
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

# The Monday that ANCHORS the era-2 rotation math (weeks_diff is measured from
# it). Not the go-live date — see NEW_ERA_GO_LIVE.
NEW_ERA_START = date(2026, 8, 17)
# The day the floor ACTUALLY switches to E/M/L/F. Moved from Mon 08-17 to
# Wed 08-19 (Juan, 2026-08-17). This is a DAY-level gate, not week-level: the
# week of 08-17 runs legacy A/B/C/D on Mon+Tue and era-2 E/M/L/F from Wed on.
# Keeping the anchor on the Monday means the week-of-08-17 assignments
# (Julie E / Jelyn M / Chris F / Sarah L) still land correctly on Wed-Sat.
NEW_ERA_GO_LIVE = date(2026, 8, 19)

# ── ERA 1 (legacy) ───────────────────────────────────────────────────────────
LEGACY_ROTATION = ["A", "B", "C", "D"]
LEGACY_REFERENCE_WEEK = date(2026, 4, 27)
LEGACY_REFERENCE_ASSIGNMENTS = {
    "Jelyn": "A",
    "Sarah": "B",
    "Chris": "C",
    "Julie": "D",
}

# ── ERA 2 ────────────────────────────────────────────────────────────────────
VET_ROTATION = ["E", "M", "L", "F"]
TRAINEE_ROTATION = ["E", "M", "L"]
NEW_REFERENCE_WEEK = NEW_ERA_START
NEW_VET_ASSIGNMENTS = {
    "Sarah": "E",
    "Jelyn": "M",
    "Chris": "L",
    "Julie": "F",
}
NEW_TRAINEE_ASSIGNMENTS = {
    "Jane": "E",       # Jane Camomot ("Jobert") — Official Calendar 2
    "Jamaica": "M",    # Jamaica Fernandez — Official Calendar 5
    "Debbie": "L",     # Debbie/"Deborah" — Official Calendar 7
}

# ── ERA 3 (pairs/units, weeks from Mon 2026-08-24) ───────────────────────────
PAIRS_ERA_START = date(2026, 8, 24)
SHIFT_UNITS = [
    ["Jane", "Julie"],      # 2026-08-24: E
    ["Jamaica", "Jelyn"],   # 2026-08-24: M
    ["Sarah", "Debbie"],    # 2026-08-24: F
    ["Christelle"],         # 2026-08-24: L
]
# Chronological block order — NOT era-2's VET_ROTATION (E, M, L, F).
UNIT_ROTATION = ["E", "M", "F", "L"]

# All block ids across both eras — importers index BLOCKS[block_id] directly.
BLOCKS = {
    "A": {"label": "8am – 2pm",  "activity": "Speed to Lead → Power Dialer", "start_h": 8,  "end_h": 14},
    "B": {"label": "9am – 3pm",  "activity": "Power Dialer → Speed to Lead → Power Dialer", "start_h": 9,  "end_h": 15},
    "C": {"label": "2pm – 8pm",  "activity": "Speed to Lead → Power Dialer", "start_h": 14, "end_h": 20},
    "D": {"label": "4pm – 10pm", "activity": "Power Dialer → Speed to Lead → Mixed Follow-up", "start_h": 16, "end_h": 22},
    "E": {"label": "8am – 2pm",  "activity": "STL point ↔ Power Dialer (hourly rotation)", "start_h": 8,  "end_h": 14},
    "M": {"label": "11am – 5pm", "activity": "STL point ↔ Power Dialer (hourly rotation)", "start_h": 11, "end_h": 17},
    "L": {"label": "4pm – 10pm", "activity": "STL point ↔ Power Dialer (hourly rotation)", "start_h": 16, "end_h": 22},
    "F": {"label": "12pm – 6pm", "activity": "Peak flex — STL point ↔ Power Dialer", "start_h": 12, "end_h": 18},
}

# Back-compat aliases (legacy importers reference these names).
ROTATION = LEGACY_ROTATION
REFERENCE_WEEK = LEGACY_REFERENCE_WEEK
REFERENCE_ASSIGNMENTS = LEGACY_REFERENCE_ASSIGNMENTS

# Sunday-crew block per era.
_LEGACY_SUNDAY_BLOCK = "B"
_NEW_SUNDAY_BLOCK = "M"


# Rep-name aliases. `_schedule.py` keys Christelle as "Chris" (her Hot Prospector
# / legacy name) but `board_reps` and `shift_overrides` store "Christelle", and
# both dashboards + lead-pipeline-sync use "Christelle" too. So her override was
# silently missing here and she fell back to the rotation block — on 2026-08-19
# this file said L (4pm-10pm) while every other surface said F (12pm-6pm).
# Look overrides up under every known spelling.
REP_ALIASES = {
    "Chris": ("Chris", "Christelle", "Chris Betuin"),
    "Christelle": ("Christelle", "Chris", "Chris Betuin"),
    "Jane": ("Jane", "Jobert"),
    "Debbie": ("Debbie", "Deborah"),
}


def _override_for(overrides, rep, monday):
    """First override matching any known spelling of `rep` for that week."""
    for name in REP_ALIASES.get(rep, (rep,)):
        ov = overrides.get(f"{name}_{monday.strftime('%Y-%m-%d')}")
        if ov:
            return ov
    return None


def _week_monday(target_date):
    return target_date - timedelta(days=target_date.weekday())


def _is_new_era(target_date):
    return target_date >= NEW_ERA_GO_LIVE


def _is_pairs_era(target_date):
    # WEEK-level gate (mirrors manager.html / shift-assigner / resolve-bridge):
    # the whole week is pairs-era iff its Monday is on/after the anchor.
    return _week_monday(target_date) >= PAIRS_ERA_START


def _unit_idx_for(rep):
    """Index into SHIFT_UNITS for rep, alias-aware — anomaly_detector's
    REP_MAP says "Chris" but the unit roster says "Christelle"."""
    names = set(REP_ALIASES.get(rep, (rep,)))
    for idx, unit in enumerate(SHIFT_UNITS):
        if names.intersection(unit):
            return idx
    return None


def block_for_rep(rep, target_date, overrides=None):
    """Which shift block is the rep on, for the week containing target_date?

    `overrides`: optional dict of `{ "Sarah_2026-04-27": "C", ... }` from the
    Shift Overrides store. A matching override for (rep, week_monday) wins.
    Valid override values are any known block id for the applicable era.
    """
    monday = _week_monday(target_date)
    new_era = _is_new_era(target_date)
    pairs_era = _is_pairs_era(target_date)

    if overrides:
        ov = _override_for(overrides, rep, monday)
        # Overrides are keyed by WEEK, but the era now flips MID-week. Validate the
        # override against the era of this specific day, or a week-keyed era-2
        # override ("Sarah -> L") would leak onto Mon/Tue and show her an E/M/L/F
        # block while the floor is still on A/B/C/D.
        era_blocks = (set(VET_ROTATION) | set(TRAINEE_ROTATION)) if (new_era or pairs_era) \
            else set(LEGACY_ROTATION)
        if ov in era_blocks:
            return ov

    if pairs_era:
        unit_idx = _unit_idx_for(rep)
        if unit_idx is None:
            return None
        weeks_diff = (monday - PAIRS_ERA_START).days // 7
        # +400 keeps parity with the TS mirrors (guards a negative weeks_diff).
        return UNIT_ROTATION[(unit_idx + weeks_diff + 400) % len(UNIT_ROTATION)]

    if new_era:
        weeks_diff = (monday - NEW_REFERENCE_WEEK).days // 7
        if rep in NEW_VET_ASSIGNMENTS:
            base_idx = VET_ROTATION.index(NEW_VET_ASSIGNMENTS[rep])
            return VET_ROTATION[(base_idx + weeks_diff) % len(VET_ROTATION)]
        if rep in NEW_TRAINEE_ASSIGNMENTS:
            base_idx = TRAINEE_ROTATION.index(NEW_TRAINEE_ASSIGNMENTS[rep])
            return TRAINEE_ROTATION[(base_idx + weeks_diff) % len(TRAINEE_ROTATION)]
        return None

    if rep not in LEGACY_REFERENCE_ASSIGNMENTS:
        return None
    weeks_diff = (monday - LEGACY_REFERENCE_WEEK).days // 7
    base_idx = LEGACY_ROTATION.index(LEGACY_REFERENCE_ASSIGNMENTS[rep])
    return LEGACY_ROTATION[(base_idx + weeks_diff) % len(LEGACY_ROTATION)]


def shift_label_for_rep(rep, target_date=None, overrides=None):
    """e.g. '9am – 3pm' for the rep's current week."""
    if target_date is None:
        target_date = datetime.now(EST).date()
    block = block_for_rep(rep, target_date, overrides)
    return BLOCKS[block]["label"] if block else None


def shift_status(rep, dt_est=None, overrides=None):
    """Return one of: 'on_shift', 'before_shift', 'after_shift', 'off_day'.

    Sundays: only the Sunday-crew block for the applicable era works
    (legacy: Block B rep · new era: both M-block reps).
    """
    if dt_est is None:
        dt_est = datetime.now(EST)
    block = block_for_rep(rep, dt_est.date(), overrides)
    if not block:
        return "off_day"
    sunday_block = _NEW_SUNDAY_BLOCK if _is_new_era(dt_est.date()) else _LEGACY_SUNDAY_BLOCK
    if dt_est.weekday() == 6 and block != sunday_block:
        return "off_day"
    b = BLOCKS[block]
    if dt_est.hour < b["start_h"]:
        return "before_shift"
    if dt_est.hour >= b["end_h"]:
        return "after_shift"
    return "on_shift"


def is_on_shift(rep, dt_est=None, overrides=None):
    return shift_status(rep, dt_est, overrides) == "on_shift"


def all_assignments_for_week(target_date=None, overrides=None):
    """Map of {rep: block_id} for the week containing target_date."""
    if target_date is None:
        target_date = datetime.now(EST).date()
    if _is_pairs_era(target_date):
        reps = [m for unit in SHIFT_UNITS for m in unit]
    elif _is_new_era(target_date):
        reps = list(NEW_VET_ASSIGNMENTS) + list(NEW_TRAINEE_ASSIGNMENTS)
    else:
        reps = list(LEGACY_REFERENCE_ASSIGNMENTS)
    return {rep: block_for_rep(rep, target_date, overrides) for rep in reps}
