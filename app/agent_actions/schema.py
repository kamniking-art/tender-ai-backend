"""Agent action types and records.

No IO, no DB, no SQLAlchemy — safe to import in pure-unit-test environments.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class ActionType(StrEnum):
    EXTRACT_DOCUMENTS   = "extract_documents"
    BUILD_CHECKLIST     = "build_checklist"
    CALCULATE_FIT_SCORE = "calculate_fit_score"
    EVALUATE_POLICIES   = "evaluate_policies"
    CREATE_ESCALATION   = "create_escalation"
    SEND_NOTIFICATION   = "send_notification"
    PREPARE_DOCUMENTS   = "prepare_documents"


class ActionRecord(BaseModel):
    """Pydantic representation of an agent action row (no SQLAlchemy)."""

    action_id: UUID
    company_id: UUID
    agent_id: UUID
    task_id: UUID | None = None
    action_type: str
    target: str | None = None
    payload: dict[str, Any] = {}
    status: str  # pending | running | completed | failed | rolled_back
    confidence: float | None = None
    rollback_possible: bool = False
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
