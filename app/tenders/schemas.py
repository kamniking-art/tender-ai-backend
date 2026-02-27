from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TenderStatus(StrEnum):
    NEW = "new"
    ANALYZING = "analyzing"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUBMITTED = "submitted"
    WON = "won"
    LOST = "lost"


class SortField(StrEnum):
    DEADLINE = "deadline"
    PUBLISHED = "published"
    NMCK = "nmck"
    CREATED = "created"


class SortOrder(StrEnum):
    ASC = "asc"
    DESC = "desc"


class TenderCreate(BaseModel):
    source: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    title: str | None = None
    customer_name: str | None = None
    region: str | None = None
    procurement_type: str | None = None
    nmck: Decimal | None = None
    published_at: datetime | None = None
    submission_deadline: datetime | None = None
    status: TenderStatus = TenderStatus.NEW


class TenderUpdate(BaseModel):
    title: str | None = None
    customer_name: str | None = None
    region: str | None = None
    procurement_type: str | None = None
    nmck: Decimal | None = None
    published_at: datetime | None = None
    submission_deadline: datetime | None = None


class TenderStatusUpdate(BaseModel):
    status: TenderStatus
    comment: str | None = None


class TenderRead(BaseModel):
    id: UUID
    company_id: UUID
    source: str
    external_id: str
    title: str | None
    customer_name: str | None
    region: str | None
    procurement_type: str | None
    nmck: Decimal | None
    published_at: datetime | None
    submission_deadline: datetime | None
    status: TenderStatus
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TenderListResponse(BaseModel):
    items: list[TenderRead]
    total: int
    limit: int
    offset: int
