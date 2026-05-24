"""Unit tests for escalation state machine and schema.

No SQLAlchemy, no DB, no FastAPI — pure logic only.
"""
from __future__ import annotations

import pytest

from app.escalation.schema import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    EscalationStateError,
    EscalationType,
    check_transition,
)


# ── 1. EscalationType enum ────────────────────────────────────────────────────


def test_escalation_type_enum():
    """EscalationType must expose exactly 4 canonical types."""
    expected = {"decision_review", "high_nmck", "deadline_urgent", "manual"}
    assert set(EscalationType) == expected


# ── 2. Terminal states immutable ──────────────────────────────────────────────


def test_terminal_states_immutable():
    """Attempting any transition OUT OF a terminal state must raise EscalationStateError."""
    targets = ["approved", "rejected", "timeout", "pending"]
    for terminal in TERMINAL_STATUSES:
        for target in targets:
            if target == terminal:
                continue  # same-state is a no-op, tested separately
            with pytest.raises(EscalationStateError):
                check_transition(terminal, target)


# ── 3. Approval idempotency ───────────────────────────────────────────────────


def test_approval_idempotent():
    """Calling check_transition on the same target status returns False (no-op)."""
    # Approved → approved is a no-op, not an error
    result = check_transition("approved", "approved")
    assert result is False

    # Same for other terminal same-to-same
    assert check_transition("rejected", "rejected") is False
    assert check_transition("timeout",  "timeout")  is False

    # Active same-to-same is also a no-op
    assert check_transition("pending",  "pending")  is False
    assert check_transition("reminded", "reminded") is False


# ── 4. Valid state transitions ────────────────────────────────────────────────


def test_state_transitions():
    """Active statuses can transition to any target status."""
    valid_active = ["pending", "reminded"]
    valid_targets = ["approved", "rejected", "timeout", "reminded"]

    for current in valid_active:
        for target in valid_targets:
            if current == target:
                assert check_transition(current, target) is False
            else:
                assert check_transition(current, target) is True, (
                    f"Expected True for {current!r} → {target!r}"
                )


# ── 5. Reminder is not a new escalation ──────────────────────────────────────


def test_reminder_not_new_escalation():
    """'reminded' must be in ACTIVE_STATUSES so create_escalation finds it
    and returns the existing record instead of creating a duplicate."""
    assert "reminded" in ACTIVE_STATUSES
    assert "reminded" not in TERMINAL_STATUSES
    # Proof: an escalation with status='reminded' would be found by the
    # idempotency query (status IN ACTIVE_STATUSES) → no new record created.
    active_check = "reminded" in ACTIVE_STATUSES
    terminal_check = "reminded" not in TERMINAL_STATUSES
    assert active_check and terminal_check
