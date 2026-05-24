"""Financial snapshot helpers — pure functions, no IO, no DB.

Safe to import in pure-unit-test environments.
Converts the float-based result of compute_finance_v2() into
DB-storable Decimal values for the tender_finance snapshot columns.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum


# ── Profitability status ──────────────────────────────────────────────────────


class ProfitabilityStatus(StrEnum):
    """Mirrors the profitability_status_enum PostgreSQL type (migration 21)."""
    GO                = "go"
    NO_GO             = "no_go"
    REQUIRES_ANALYSIS = "requires_analysis"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _to_decimal_or_none(value: object) -> Decimal | None:
    """Convert float/int/str/Decimal to Decimal, or None on failure."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ── Main builder ──────────────────────────────────────────────────────────────


def build_finance_snapshot(
    finance_result: dict,
    *,
    contract_value: object = None,
    cost_estimate: object = None,
    participation_cost: object = None,
    win_probability: object = None,
) -> dict:
    """Convert compute_finance_v2() result dict to DB-storable snapshot fields.

    Args:
        finance_result:     dict returned by compute_finance_v2(). Contains float
                            values for gross_margin, gross_margin_pct, expected_value
                            and a string finance_recommendation.
        contract_value:     Original contract price (NMCK) used in the calculation.
        cost_estimate:      Cost-estimate input.
        participation_cost: Participation-cost input.
        win_probability:    Win-probability percentage input.

    Returns:
        Dict with keys:
            profitability_status       str | None   — go / no_go / requires_analysis
            is_loss_leader             bool          — True when gross_margin <= 0
            gross_margin               Decimal|None  — NUMERIC(14,2)
            gross_margin_pct           Decimal|None  — NUMERIC(7,4)
            expected_value             Decimal|None  — NUMERIC(14,2)
            finance_calculated_at      datetime      — UTC now()
            snapshot_contract_value    Decimal|None  — NUMERIC(14,2)
            snapshot_cost_estimate     Decimal|None  — NUMERIC(14,2)
            snapshot_participation_cost Decimal|None — NUMERIC(14,2)
            snapshot_win_probability   Decimal|None  — NUMERIC(5,2)
    """
    gross_margin = _to_decimal_or_none(finance_result.get("gross_margin"))
    gross_margin_pct = _to_decimal_or_none(finance_result.get("gross_margin_pct"))
    expected_value = _to_decimal_or_none(finance_result.get("expected_value"))
    profitability_status: str | None = finance_result.get("finance_recommendation")

    # is_loss_leader: gross_margin present AND <= 0
    is_loss_leader: bool = (
        gross_margin is not None and gross_margin <= Decimal("0")
    )

    return {
        "profitability_status": profitability_status,
        "is_loss_leader": is_loss_leader,
        "gross_margin": gross_margin,
        "gross_margin_pct": gross_margin_pct,
        "expected_value": expected_value,
        "finance_calculated_at": datetime.now(timezone.utc),
        # Snapshot inputs — traceable record of what was fed into the calculation.
        "snapshot_contract_value":     _to_decimal_or_none(contract_value),
        "snapshot_cost_estimate":      _to_decimal_or_none(cost_estimate),
        "snapshot_participation_cost": _to_decimal_or_none(participation_cost),
        "snapshot_win_probability":    _to_decimal_or_none(win_probability),
    }
