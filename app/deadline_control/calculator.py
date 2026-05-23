"""Pure deadline calculation — no IO, no DB, no SQLAlchemy.

Import this module in tests and anywhere calculate_status() is needed
without pulling in the SQLAlchemy-heavy service.py.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def calculate_status(deadline: datetime | None) -> dict:
    """Compute deadline status from a submission_deadline datetime.

    Args:
        deadline: aware or naive UTC datetime, or None.

    Returns:
        {
            "hours_remaining": int | None,   # None only when deadline is None
            "deadline_status": str,          # safe | warning | urgent | expired
            "can_recommend_go": bool,
        }

    Status table (boundary values are inclusive on the upper bound):
        deadline IS NULL          → safe,    can_recommend_go=True
        hours_remaining > 72      → safe,    can_recommend_go=True
        72 >= hours_remaining > 24 → warning, can_recommend_go=True
        24 >= hours_remaining > 0  → urgent,  can_recommend_go=False
        hours_remaining <= 0      → expired, can_recommend_go=False
    """
    if deadline is None:
        return {
            "hours_remaining": None,
            "deadline_status": "safe",
            "can_recommend_go": True,
        }

    now = datetime.now(timezone.utc)

    # Normalise naive datetimes to UTC
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    diff_seconds = (deadline - now).total_seconds()
    hours_remaining = math.floor(diff_seconds / 3600)

    if hours_remaining <= 0:
        return {
            "hours_remaining": hours_remaining,
            "deadline_status": "expired",
            "can_recommend_go": False,
        }
    if hours_remaining <= 24:
        return {
            "hours_remaining": hours_remaining,
            "deadline_status": "urgent",
            "can_recommend_go": False,
        }
    if hours_remaining <= 72:
        return {
            "hours_remaining": hours_remaining,
            "deadline_status": "warning",
            "can_recommend_go": True,
        }
    return {
        "hours_remaining": hours_remaining,
        "deadline_status": "safe",
        "can_recommend_go": True,
    }
