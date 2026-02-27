from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class TenderAnalysisStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    APPROVED = "approved"


class TenderAnalysisPatchStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"


class TenderAnalysisCreate(BaseModel):
    requirements: dict | None = None
    missing_docs: list | None = None
    risk_flags: list | None = None
    summary: str | None = None
    overwrite: bool = False


class TenderAnalysisPatch(BaseModel):
    requirements: dict | None = None
    missing_docs: list | None = None
    risk_flags: list | None = None
    summary: str | None = None
    status: TenderAnalysisPatchStatus | None = None


class TenderAnalysisRead(BaseModel):
    id: UUID
    tender_id: UUID
    status: Literal["draft", "ready", "approved"]
    requirements: dict
    missing_docs: list
    risk_flags: list
    summary: str | None
    created_by: UUID | None
    updated_by: UUID | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
