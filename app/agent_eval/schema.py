"""Agent evaluation schema — pure functions, no IO, no DB.

Safe to import in pure-unit-test environments.
"""
from __future__ import annotations

from enum import StrEnum


# ── Enums ─────────────────────────────────────────────────────────────────────


class AgentRecommendation(StrEnum):
    """What the AI agent recommended for a tender."""
    GO           = "go"
    NO_GO        = "no_go"
    NEEDS_REVIEW = "needs_review"


class HumanDecision(StrEnum):
    """What the human/manager ultimately decided."""
    PARTICIPATE = "participate"
    SKIP        = "skip"
    DEFERRED    = "deferred"


class ActualResult(StrEnum):
    """Actual outcome of the tender participation."""
    WON           = "won"
    LOST          = "lost"
    CANCELLED     = "cancelled"
    NOT_SUBMITTED = "not_submitted"


# ── Pure helpers ──────────────────────────────────────────────────────────────


def compute_was_right(
    recommendation: str,
    human_decision: str | None = None,
    actual_result: str | None = None,
) -> bool | None:
    """Determine whether the agent recommendation was correct.

    Rules:
        GO        + participate + won          → True  (right call)
        GO        + participate + lost         → False (overestimated)
        GO        + skip                       → False (agent said go, human skipped)
        NO_GO     + skip                       → True  (conservative, avoided a loss)
        NO_GO     + participate + won          → False (agent was too pessimistic)
        NO_GO     + participate + lost         → True  (agent predicted correctly)
        NO_GO     + participate + not_submitted→ None  (inconclusive)
        NEEDS_REVIEW + deferred               → True  (correctly flagged uncertainty)
        NEEDS_REVIEW + *                      → None  (inconclusive without follow-up)
        Any + cancelled / not_submitted       → None  (inconclusive)
        Missing human_decision or actual_result for GO/NO_GO → None

    Returns:
        True  — agent was correct
        False — agent was wrong
        None  — outcome is inconclusive or not yet known
    """
    if actual_result in (ActualResult.CANCELLED, ActualResult.NOT_SUBMITTED):
        return None

    if recommendation == AgentRecommendation.NEEDS_REVIEW:
        if human_decision == HumanDecision.DEFERRED:
            return True
        return None

    if recommendation == AgentRecommendation.GO:
        if human_decision == HumanDecision.SKIP:
            return False
        if human_decision == HumanDecision.PARTICIPATE:
            if actual_result == ActualResult.WON:
                return True
            if actual_result == ActualResult.LOST:
                return False
        return None

    if recommendation == AgentRecommendation.NO_GO:
        if human_decision == HumanDecision.SKIP:
            return True
        if human_decision == HumanDecision.PARTICIPATE:
            if actual_result == ActualResult.WON:
                return False
            if actual_result == ActualResult.LOST:
                return True
        return None

    return None
