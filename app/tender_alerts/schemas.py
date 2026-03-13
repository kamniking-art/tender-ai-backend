from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class AlertCategory(StrEnum):
    NEW = "new"
    DEADLINE_SOON = "deadline_soon"
    RISKY = "risky"
    GO = "go"
    NO_GO = "no_go"
    OVERDUE_TASK = "overdue_task"


class AlertTenderItem(BaseModel):
    tender_id: UUID
    title: str | None = None
    category: AlertCategory
    deadline_at: datetime | None = None
    risk_score: int | None = None
    recommendation: Literal["strong_go", "go", "review", "weak", "no_go", "unsure"] | None = None


class AlertCounts(BaseModel):
    new: int = 0
    deadline_soon: int = 0
    risky: int = 0
    go: int = 0
    no_go: int = 0
    overdue_task: int = 0


class AlertDigestResponse(BaseModel):
    counts: AlertCounts
    items: list[AlertTenderItem]


class AlertSummaryResponse(BaseModel):
    counts: AlertCounts


class AlertAckRequest(BaseModel):
    category: AlertCategory


class AlertAckResponse(BaseModel):
    ok: bool = True
