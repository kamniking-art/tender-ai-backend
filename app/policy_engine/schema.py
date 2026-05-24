from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


class SkipReason(str, Enum):
    FACT_MISSING = "fact_missing"
    INACTIVE = "inactive"
    INVALID = "invalid"
    CONDITION_NOT_MET = "condition_not_met"


class FactField(str, Enum):
    DEADLINE_HOURS_REMAINING = "deadline_hours_remaining"
    FIT_SCORE = "fit_score"
    SRO_OK = "sro_ok"
    LICENSE_OK = "license_ok"
    NMCK = "nmck"
    REQUIREMENT_STATUS = "requirement_status"
    OKVED_MATCH = "okved_match"
    FUNDS_OK = "funds_ok"


class Operator(str, Enum):
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    EQ = "eq"
    NEQ = "neq"
    IS_TRUE = "is_true"
    IS_FALSE = "is_false"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"


class ActionType(str, Enum):
    BLOCK_RECOMMENDATION = "block_recommendation"
    ADD_RISK_FLAG = "add_risk_flag"
    REQUIRE_APPROVAL = "require_approval"
    ADJUST_SCORE = "adjust_score"
    NOTIFY = "notify"
    SKIP_TENDER = "skip_tender"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_ACTION_SEVERITY: dict[ActionType, Severity] = {
    ActionType.BLOCK_RECOMMENDATION: Severity.CRITICAL,
    ActionType.SKIP_TENDER: Severity.CRITICAL,
    ActionType.ADD_RISK_FLAG: Severity.WARNING,
    ActionType.REQUIRE_APPROVAL: Severity.WARNING,
    ActionType.ADJUST_SCORE: Severity.INFO,
    ActionType.NOTIFY: Severity.INFO,
}


def action_severity(action_type: ActionType) -> Severity:
    return _ACTION_SEVERITY.get(action_type, Severity.INFO)


class PolicyCondition(BaseModel):
    field: FactField
    operator: Operator
    value: Any = None


class PolicyAction(BaseModel):
    type: ActionType
    payload: dict[str, Any] = {}


class PolicySchema(BaseModel):
    policy_id: UUID
    company_id: UUID
    policy_type: str
    condition: PolicyCondition
    action: PolicyAction
    priority: int = 0
    active: bool = True
    created_at: datetime | None = None


class PolicyEvaluationResult(BaseModel):
    policy_id: UUID
    policy_type: str
    passed: bool
    severity: Severity
    score_delta: float | None
    action_type: ActionType
    evidence: dict[str, Any]
    explanation: str
    evaluated_at: datetime


class PolicyExecutionTrace(BaseModel):
    policy_id: UUID
    policy_type: str
    passed: bool | None          # None if skipped before evaluation
    skipped: bool
    skip_reason: SkipReason | None
    action_type: ActionType | None
    severity: Severity | None
    score_delta: float | None
    evidence: dict[str, Any]
    explanation: str
    evaluated_at: datetime
