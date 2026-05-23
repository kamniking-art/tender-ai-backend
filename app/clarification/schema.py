"""Clarification question workflow helpers — pure functions, no IO, no DB.

Safe to import in pure-unit-test environments.
"""
from __future__ import annotations

from enum import StrEnum

# ── Status constants ──────────────────────────────────────────────────────────

CLARIFICATION_TERMINAL: frozenset[str] = frozenset({"answered", "timeout"})
CLARIFICATION_ACTIVE:   frozenset[str] = frozenset({"draft", "approved", "sent"})

# Allowed source statuses for each action.
_TRANSITION_RULES: dict[str, frozenset[str]] = {
    "approve": frozenset({"draft"}),
    "send":    frozenset({"approved"}),   # mark_sent — only after approval
    "answer":  frozenset({"sent"}),
    "timeout": frozenset({"draft", "approved", "sent"}),
}


# ── Exceptions ────────────────────────────────────────────────────────────────


class ClarificationStateError(Exception):
    """Raised when an invalid state transition is attempted."""


# ── Enum ──────────────────────────────────────────────────────────────────────


class ClarificationStatus(StrEnum):
    DRAFT    = "draft"
    APPROVED = "approved"
    SENT     = "sent"
    ANSWERED = "answered"
    TIMEOUT  = "timeout"


# ── Pure helpers ──────────────────────────────────────────────────────────────


def is_terminal(status: str) -> bool:
    """Return True if the question is in a terminal (immutable) state."""
    return status in CLARIFICATION_TERMINAL


def is_sendable(status: str) -> bool:
    """Return True if the question can be marked as sent (must be approved first)."""
    return status == ClarificationStatus.APPROVED


def check_clarification_transition(current: str, action: str) -> bool:
    """Validate a state transition for the clarification workflow.

    Args:
        current: Current status string.
        action:  One of "approve", "send", "answer", "timeout".

    Returns:
        True  — transition is valid, caller should proceed.

    Raises:
        ClarificationStateError — transition is forbidden:
            • terminal state cannot be changed
            • action not allowed from current status
            (e.g. "send" from "draft" requires approval first)
    """
    if is_terminal(current):
        raise ClarificationStateError(
            f"Status '{current}' is terminal and cannot be changed"
        )
    allowed_from = _TRANSITION_RULES.get(action, frozenset())
    if current not in allowed_from:
        raise ClarificationStateError(
            f"Cannot perform '{action}' from status '{current}' "
            f"(allowed from: {sorted(allowed_from)})"
        )
    return True
