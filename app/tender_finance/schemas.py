from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TenderFinanceUpsert(BaseModel):
    cost_estimate: Decimal | None = Field(default=None, ge=0)
    participation_cost: Decimal | None = Field(default=None, ge=0)
    win_probability: Decimal | None = Field(default=None, ge=0, le=100)
    notes: str | None = None


class TenderFinanceRead(BaseModel):
    id: UUID
    tender_id: UUID
    cost_estimate: Decimal | None
    participation_cost: Decimal | None
    win_probability: Decimal | None
    notes: str | None
    # Financial snapshot output fields (migration 20260522_20):
    profitability_status: str | None = None
    is_loss_leader: bool = False
    gross_margin: Decimal | None = None
    gross_margin_pct: Decimal | None = None
    expected_value: Decimal | None = None
    finance_calculated_at: datetime | None = None
    # Snapshot input fields (migration 20260522_21):
    snapshot_contract_value: Decimal | None = None
    snapshot_cost_estimate: Decimal | None = None
    snapshot_participation_cost: Decimal | None = None
    snapshot_win_probability: Decimal | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
