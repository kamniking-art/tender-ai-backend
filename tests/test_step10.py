"""Unit tests for Step 10 — Clarification Questions + Agent Evaluation.

No SQLAlchemy, no DB, no FastAPI — pure logic only.
"""
from __future__ import annotations

import pytest

from app.clarification.schema import (
    CLARIFICATION_ACTIVE,
    CLARIFICATION_TERMINAL,
    ClarificationStateError,
    ClarificationStatus,
    check_clarification_transition,
    is_sendable,
    is_terminal,
)
from app.agent_eval.schema import (
    ActualResult,
    AgentRecommendation,
    HumanDecision,
    compute_was_right,
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. ClarificationStatus enum values
# ═══════════════════════════════════════════════════════════════════════════════


def test_clarification_status_enum_values():
    """ClarificationStatus must expose exactly 5 values."""
    expected = {"draft", "approved", "sent", "answered", "timeout"}
    assert set(ClarificationStatus) == expected


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Terminal vs active sets
# ═══════════════════════════════════════════════════════════════════════════════


def test_terminal_and_active_are_disjoint():
    """TERMINAL and ACTIVE sets must not overlap."""
    assert CLARIFICATION_TERMINAL.isdisjoint(CLARIFICATION_ACTIVE)


def test_terminal_statuses():
    assert CLARIFICATION_TERMINAL == frozenset({"answered", "timeout"})


def test_active_statuses():
    assert CLARIFICATION_ACTIVE == frozenset({"draft", "approved", "sent"})


# ═══════════════════════════════════════════════════════════════════════════════
# 3. is_terminal / is_sendable helpers
# ═══════════════════════════════════════════════════════════════════════════════


def test_is_terminal_true():
    for s in ("answered", "timeout"):
        assert is_terminal(s) is True


def test_is_terminal_false():
    for s in ("draft", "approved", "sent"):
        assert is_terminal(s) is False


def test_is_sendable_only_approved():
    assert is_sendable("approved") is True
    for s in ("draft", "sent", "answered", "timeout"):
        assert is_sendable(s) is False, f"is_sendable({s!r}) should be False"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. State machine — valid transitions
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("current,action", [
    ("draft",    "approve"),
    ("approved", "send"),
    ("sent",     "answer"),
    ("draft",    "timeout"),
    ("approved", "timeout"),
    ("sent",     "timeout"),
])
def test_valid_transitions(current: str, action: str):
    """Valid transitions must return True."""
    assert check_clarification_transition(current, action) is True


# ═══════════════════════════════════════════════════════════════════════════════
# 5. State machine — terminal states block all actions
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("terminal", ["answered", "timeout"])
@pytest.mark.parametrize("action", ["approve", "send", "answer", "timeout"])
def test_terminal_blocks_all_actions(terminal: str, action: str):
    """Any action on a terminal status must raise ClarificationStateError."""
    with pytest.raises(ClarificationStateError):
        check_clarification_transition(terminal, action)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. State machine — invalid transitions (wrong order)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("current,action", [
    ("draft",    "send"),    # must approve first
    ("draft",    "answer"),  # must go through approve → send first
    ("sent",     "approve"), # already sent
    ("approved", "answer"),  # not sent yet
])
def test_invalid_transitions(current: str, action: str):
    """Invalid transitions must raise ClarificationStateError."""
    with pytest.raises(ClarificationStateError):
        check_clarification_transition(current, action)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. AgentRecommendation / HumanDecision / ActualResult enums
# ═══════════════════════════════════════════════════════════════════════════════


def test_agent_recommendation_enum():
    assert set(AgentRecommendation) == {"go", "no_go", "needs_review"}


def test_human_decision_enum():
    assert set(HumanDecision) == {"participate", "skip", "deferred"}


def test_actual_result_enum():
    assert set(ActualResult) == {"won", "lost", "cancelled", "not_submitted"}


# ═══════════════════════════════════════════════════════════════════════════════
# 8. compute_was_right — correctness rules
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("recommendation,human_decision,actual_result,expected", [
    # GO cases
    ("go",        "participate", "won",           True),
    ("go",        "participate", "lost",          False),
    ("go",        "skip",        None,            False),
    # NO_GO cases
    ("no_go",     "skip",        None,            True),
    ("no_go",     "participate", "won",           False),
    ("no_go",     "participate", "lost",          True),
    # NEEDS_REVIEW cases
    ("needs_review", "deferred", None,            True),
    ("needs_review", "participate", "won",        None),
    # Inconclusive — cancelled / not_submitted
    ("go",        "participate", "cancelled",     None),
    ("go",        "participate", "not_submitted", None),
    ("no_go",     "participate", "cancelled",     None),
    # Missing data
    ("go",        None,          None,            None),
    ("no_go",     None,          None,            None),
])
def test_compute_was_right(
    recommendation: str,
    human_decision: str | None,
    actual_result: str | None,
    expected: bool | None,
):
    result = compute_was_right(recommendation, human_decision, actual_result)
    assert result == expected, (
        f"compute_was_right({recommendation!r}, {human_decision!r}, {actual_result!r}) "
        f"returned {result!r}, expected {expected!r}"
    )
