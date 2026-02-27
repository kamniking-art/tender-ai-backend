from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


TaskType = Literal[
    "clarification_deadline",
    "submission_deadline",
    "bid_security_deadline",
    "contract_security_deadline",
    "contract_signing_deadline",
    "other",
]
TaskStatus = Literal["pending", "done", "overdue"]
TaskOrderBy = Literal["due_at asc", "due_at desc"]


class TenderTaskCreate(BaseModel):
    type: TaskType
    title: str
    description: str | None = None
    due_at: datetime


class TenderTaskUpdate(BaseModel):
    type: TaskType | None = None
    title: str | None = None
    description: str | None = None
    due_at: datetime | None = None
    status: TaskStatus | None = None


class TenderTaskRead(BaseModel):
    id: UUID
    tender_id: UUID
    type: TaskType
    title: str
    description: str | None
    due_at: datetime
    status: TaskStatus
    created_by: UUID
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
