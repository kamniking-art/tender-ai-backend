"""Unit tests for agent_actions schema and reasoning decision mapping.

No SQLAlchemy, no DB, no FastAPI — pure logic only.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.agent_actions.schema import ActionRecord, ActionType
from app.reasoning.schema import map_recommendation_to_decision


# ── ActionType enum ────────────────────────────────────────────────────────────


def test_action_type_all_seven_values():
    """ActionType enum must expose exactly 7 canonical action types."""
    expected = {
        "extract_documents",
        "build_checklist",
        "calculate_fit_score",
        "evaluate_policies",
        "create_escalation",
        "send_notification",
        "prepare_documents",
    }
    assert set(ActionType) == expected


# ── ActionRecord Pydantic model ────────────────────────────────────────────────


def test_action_record_minimal_construction():
    """ActionRecord can be constructed with the minimum required fields."""
    record = ActionRecord(
        action_id=uuid.uuid4(),
        company_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        action_type=ActionType.EXTRACT_DOCUMENTS,
        status="running",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert record.status == "running"
    assert record.task_id is None
    assert record.target is None
    assert record.payload == {}
    assert record.confidence is None
    assert record.rollback_possible is False
    assert record.result is None


def test_action_record_full_construction():
    """ActionRecord correctly stores all optional fields."""
    aid = uuid.uuid4()
    cid = uuid.uuid4()
    tid = uuid.uuid4()
    now = datetime.now(timezone.utc)
    record = ActionRecord(
        action_id=aid,
        company_id=cid,
        agent_id=uuid.uuid4(),
        task_id=tid,
        action_type=ActionType.EVALUATE_POLICIES,
        target="tender-123",
        payload={"policy_count": 3},
        status="completed",
        confidence=0.85,
        rollback_possible=True,
        result={"status": "ok", "triggered": 2},
        created_at=now,
        updated_at=now,
    )
    assert record.action_id == aid
    assert record.company_id == cid
    assert record.task_id == tid
    assert record.target == "tender-123"
    assert record.payload == {"policy_count": 3}
    assert record.confidence == pytest.approx(0.85)
    assert record.rollback_possible is True
    assert record.result == {"status": "ok", "triggered": 2}


# ── map_recommendation_to_decision ────────────────────────────────────────────


@pytest.mark.parametrize("recommendation,expected", [
    ("strong_go",  "GO"),
    ("go",         "GO"),
    ("no_go",      "NO_GO"),
    ("review",     "NEEDS_REVIEW"),
    ("weak",       "NEEDS_REVIEW"),
    (None,         None),
    ("unknown_val", None),
])
def test_map_recommendation_to_decision(recommendation, expected):
    """Recommendation strings map to canonical decision labels correctly."""
    assert map_recommendation_to_decision(recommendation) == expected
