"""Unit tests for financial snapshot logic and escalation timeout check.

No SQLAlchemy, no DB, no FastAPI — pure logic only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.tender_finance.snapshot import build_finance_snapshot
from app.escalation.schema import is_escalation_stale


# ── 1. Profitability: go ──────────────────────────────────────────────────────


def test_profitability_go():
    """go recommendation → profitability_status='go', is_loss_leader=False."""
    result = {
        "finance_recommendation": "go",
        "gross_margin": 150_000.0,
        "gross_margin_pct": 15.0,
        "expected_value": 90_000.0,
    }
    snap = build_finance_snapshot(result)
    assert snap["profitability_status"] == "go"
    assert snap["is_loss_leader"] is False
    assert snap["gross_margin"] == Decimal("150000.0")
    assert snap["gross_margin_pct"] == Decimal("15.0")
    assert snap["expected_value"] == Decimal("90000.0")


# ── 2. Profitability: no_go with loss ─────────────────────────────────────────


def test_profitability_no_go_loss():
    """Negative gross_margin → no_go, is_loss_leader=True."""
    result = {
        "finance_recommendation": "no_go",
        "gross_margin": -20_000.0,
        "gross_margin_pct": -5.0,
        "expected_value": -25_000.0,
    }
    snap = build_finance_snapshot(result)
    assert snap["profitability_status"] == "no_go"
    assert snap["is_loss_leader"] is True
    assert snap["gross_margin"] < Decimal("0")


# ── 3. Profitability: requires_analysis ──────────────────────────────────────


def test_profitability_requires_analysis():
    """Missing cost_estimate → requires_analysis, all monetary fields None."""
    result = {
        "finance_recommendation": "requires_analysis",
        "gross_margin": None,
        "gross_margin_pct": None,
        "expected_value": None,
    }
    snap = build_finance_snapshot(result)
    assert snap["profitability_status"] == "requires_analysis"
    assert snap["is_loss_leader"] is False  # gross_margin is None → not a loss
    assert snap["gross_margin"] is None
    assert snap["expected_value"] is None


# ── 4. Financial fields are Decimal, not float ────────────────────────────────


def test_finance_fields_are_decimal():
    """build_finance_snapshot must return Decimal for all monetary values."""
    result = {
        "finance_recommendation": "go",
        "gross_margin": 50_000.50,
        "gross_margin_pct": 12.5,
        "expected_value": 30_000.25,
    }
    snap = build_finance_snapshot(result)
    assert isinstance(snap["gross_margin"], Decimal), (
        f"gross_margin should be Decimal, got {type(snap['gross_margin'])}"
    )
    assert isinstance(snap["gross_margin_pct"], Decimal), (
        f"gross_margin_pct should be Decimal, got {type(snap['gross_margin_pct'])}"
    )
    assert isinstance(snap["expected_value"], Decimal), (
        f"expected_value should be Decimal, got {type(snap['expected_value'])}"
    )
    # Verify float input was NOT silently kept as float
    assert not isinstance(snap["gross_margin"], float)
    assert not isinstance(snap["expected_value"], float)


# ── 5. Timeout scheduler staleness check ──────────────────────────────────────


def test_timeout_scheduler_finds_stale():
    """Escalations older than timeout_hours must be identified as stale."""
    timeout_hours = 48
    now = datetime.now(timezone.utc)

    # 49 hours ago → stale
    old_created_at = now - timedelta(hours=49)
    assert is_escalation_stale(old_created_at, timeout_hours) is True

    # Exactly at cutoff (47h59m ago) → not stale yet
    recent_created_at = now - timedelta(hours=47, minutes=59)
    assert is_escalation_stale(recent_created_at, timeout_hours) is False

    # 1 hour ago → not stale
    fresh_created_at = now - timedelta(hours=1)
    assert is_escalation_stale(fresh_created_at, timeout_hours) is False
