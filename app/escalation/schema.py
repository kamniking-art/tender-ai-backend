"""Escalation types, state machine helpers, and Pydantic record model.

No IO, no DB, no SQLAlchemy — safe to import in pure-unit-test environments.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

# ── Status sets ───────────────────────────────────────────────────────────────

TERMINAL_STATUSES: frozenset[str] = frozenset({"approved", "rejected", "timeout"})
ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "reminded"})


# ── Exceptions ────────────────────────────────────────────────────────────────


class EscalationStateError(Exception):
    """Raised when an invalid state transition is attempted."""


# ── State machine ─────────────────────────────────────────────────────────────


def check_transition(current_status: str, target_status: str) -> bool:
    """Validate a status transition for the Escalation state machine.

    Returns:
        True  — transition is valid, caller should proceed.
        False — already in *target_status* (idempotent no-op).

    Raises:
        EscalationStateError — *current_status* is terminal; cannot change.

    Valid transitions (non-exhaustive):
        pending  → approved | rejected | reminded | timeout
        reminded → approved | rejected | timeout
        approved → (terminal — immutable)
        rejected → (terminal — immutable)
        timeout  → (terminal — immutable)
    """
    if current_status == target_status:
        return False  # already there — idempotent no-op
    if current_status in TERMINAL_STATUSES:
        raise EscalationStateError(
            f"Cannot transition from terminal status '{current_status}' "
            f"to '{target_status}'"
        )
    return True


# ── Enum ──────────────────────────────────────────────────────────────────────


class EscalationType(StrEnum):
    DECISION_REVIEW  = "decision_review"
    HIGH_NMCK        = "high_nmck"
    DEADLINE_URGENT  = "deadline_urgent"
    MANUAL           = "manual"


# ── Pydantic record (no ORM dependency) ──────────────────────────────────────


def is_escalation_stale(created_at: datetime, timeout_hours: int) -> bool:
    """Return True if an escalation has exceeded its timeout threshold.

    Pure function — no DB, no IO. Used by EscalationTimeoutScheduler and tests.

    Args:
        created_at:    Creation timestamp (timezone-aware).
        timeout_hours: Number of hours before an active escalation times out.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=timeout_hours)
    return created_at < cutoff


class EscalationRecord(BaseModel):
    """Pydantic representation of an escalation row (no SQLAlchemy)."""

    escalation_id: UUID
    company_id: UUID
    agent_id: UUID | None = None
    reason: str
    confidence: float | None = None
    status: str  # pending | reminded | approved | rejected | timeout
    approved_by: UUID | None = None
    approved_at: datetime | None = None
    override_note: str | None = None
    escalation_type: str | None = None
    payload: dict[str, Any] = {}
    telegram_message_id: str | None = None
    created_at: datetime
    updated_at: datetime
