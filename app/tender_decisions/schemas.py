from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


Recommendation = Literal["strong_go", "go", "review", "weak", "no_go", "unsure"]


class TenderDecisionCreate(BaseModel):
    recommendation: Recommendation = "unsure"
    rationale: list = Field(default_factory=list)
    assumptions: list = Field(default_factory=list)

    nmck: Decimal | None = None
    expected_revenue: Decimal | None = None
    cogs: Decimal | None = None
    logistics_cost: Decimal | None = None
    other_costs: Decimal | None = None

    risk_score: int = Field(default=0, ge=0, le=100)
    risk_flags: list = Field(default_factory=list)

    need_bid_security: bool = False
    bid_security_amount: Decimal | None = None
    need_contract_security: bool = False
    contract_security_amount: Decimal | None = None

    notes: str | None = None


class TenderDecisionPatch(BaseModel):
    recommendation: Recommendation | None = None
    rationale: list | None = None
    assumptions: list | None = None

    nmck: Decimal | None = None
    expected_revenue: Decimal | None = None
    cogs: Decimal | None = None
    logistics_cost: Decimal | None = None
    other_costs: Decimal | None = None

    risk_score: int | None = Field(default=None, ge=0, le=100)
    risk_flags: list | None = None

    need_bid_security: bool | None = None
    bid_security_amount: Decimal | None = None
    need_contract_security: bool | None = None
    contract_security_amount: Decimal | None = None

    notes: str | None = None


class TenderDecisionRecommend(BaseModel):
    recommendation: Recommendation
    notes: str | None = None


class TenderDecisionRead(BaseModel):
    id: UUID
    tender_id: UUID
    recommendation: Recommendation
    rationale: list
    assumptions: list

    nmck: Decimal | None
    expected_revenue: Decimal | None
    cogs: Decimal | None
    logistics_cost: Decimal | None
    other_costs: Decimal | None
    expected_margin_value: Decimal | None
    expected_margin_pct: Decimal | None

    risk_score: int
    risk_flags: list
    decision_score: int | None
    recommendation_reason: str | None
    engine_meta: dict

    need_bid_security: bool
    bid_security_amount: Decimal | None
    need_contract_security: bool
    contract_security_amount: Decimal | None

    notes: str | None

    created_by: UUID | None
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
