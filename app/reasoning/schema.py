"""Reasoning trace pure helpers.

No IO, no DB, no SQLAlchemy — safe to import in pure-unit-test environments.
"""
from __future__ import annotations

_RECOMMENDATION_TO_DECISION: dict[str, str] = {
    "strong_go": "GO",
    "go":        "GO",
    "no_go":     "NO_GO",
    "review":    "NEEDS_REVIEW",
    "weak":      "NEEDS_REVIEW",
}


def map_recommendation_to_decision(recommendation: str | None) -> str | None:
    """Map engine recommendation string to canonical decision label.

    "strong_go" | "go"     → "GO"
    "no_go"                → "NO_GO"
    "review"   | "weak"    → "NEEDS_REVIEW"
    None or unknown value  → None
    """
    if recommendation is None:
        return None
    return _RECOMMENDATION_TO_DECISION.get(recommendation.lower())
