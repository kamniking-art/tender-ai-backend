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
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
