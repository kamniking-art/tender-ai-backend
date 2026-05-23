"""Unit tests for deadline_control.service.calculate_status().

Pure function tests — no DB required.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.deadline_control.calculator import calculate_status


def _future(hours: float) -> datetime:
    """Return a UTC datetime that is exactly `hours` hours from now."""
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _past(hours: float) -> datetime:
    """Return a UTC datetime that is `hours` hours in the past."""
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# ── 1. safe — hours > 72 ──────────────────────────────────────────────────────

def test_safe_status():
    result = calculate_status(_future(96))
    assert result["deadline_status"] == "safe"
    assert result["can_recommend_go"] is True
    assert result["hours_remaining"] is not None
    assert result["hours_remaining"] > 72


# ── 2. warning — 24 < hours <= 72 ────────────────────────────────────────────

def test_warning_status():
    result = calculate_status(_future(48))
    assert result["deadline_status"] == "warning"
    assert result["can_recommend_go"] is True
    assert result["hours_remaining"] is not None
    assert 24 < result["hours_remaining"] <= 72


# ── 3. urgent — 0 < hours <= 24 ──────────────────────────────────────────────

def test_urgent_status():
    result = calculate_status(_future(12))
    assert result["deadline_status"] == "urgent"
    assert result["can_recommend_go"] is False
    assert result["hours_remaining"] is not None
    assert 0 < result["hours_remaining"] <= 24


# ── 4. expired — deadline in the past ────────────────────────────────────────

def test_expired_status():
    result = calculate_status(_past(5))
    assert result["deadline_status"] == "expired"
    assert result["can_recommend_go"] is False
    assert result["hours_remaining"] is not None
    assert result["hours_remaining"] <= 0


# ── 5. no deadline (None) ─────────────────────────────────────────────────────

def test_no_deadline():
    result = calculate_status(None)
    assert result["deadline_status"] == "safe"
    assert result["can_recommend_go"] is True
    assert result["hours_remaining"] is None


# ── 6. boundary: exactly 72h → warning (not safe) ────────────────────────────

def test_boundary_72h():
    # 72 hours exactly: 72 >= hours > 24 → warning
    result = calculate_status(_future(72))
    # math.floor of exactly 72.0 seconds = 72; condition: hours_remaining <= 72 → warning
    assert result["deadline_status"] == "warning"
    assert result["can_recommend_go"] is True


# ── 7. boundary: exactly 24h → urgent (not warning) ──────────────────────────

def test_boundary_24h():
    # 24 hours exactly: 24 >= hours > 0 → urgent
    result = calculate_status(_future(24))
    # math.floor of exactly 24.0 = 24; condition: hours_remaining <= 24 → urgent
    assert result["deadline_status"] == "urgent"
    assert result["can_recommend_go"] is False
