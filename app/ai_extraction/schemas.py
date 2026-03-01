from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExtractedTenderV1(BaseModel):
    schema_version: Literal["v1"] = "v1"
    subject: str | None = None
    nmck: Decimal | None = None
    currency: Literal["RUB"] | None = None
    submission_deadline_at: datetime | None = None
    bid_security_required: bool | None = None
    bid_security_amount: Decimal | None = None
    bid_security_pct: Decimal | None = None
    contract_security_required: bool | None = None
    contract_security_amount: Decimal | None = None
    contract_security_pct: Decimal | None = None
    qualification_requirements: list[str] = Field(default_factory=list)
    tech_parameters: list[str] = Field(default_factory=list)
    penalties: list[str] = Field(default_factory=list)
    confidence: dict[str, float] = Field(default_factory=dict)
    evidence: dict[str, str | None] = Field(default_factory=dict)


class ExtractionRequest(BaseModel):
    document_ids: list[UUID] | None = None


class ExtractionResponse(BaseModel):
    analysis_status: Literal["draft", "ready", "approved"]
    risk_flags: list
    extracted: ExtractedTenderV1
    summary: str | None = None


class ExtractedReadResponse(BaseModel):
    extracted: ExtractedTenderV1 | None


class RemoteExtractorPayload(BaseModel):
    tender_id: UUID
    text: str
    lang: Literal["ru"] = "ru"
    schema_version: Literal["v1"] = "v1"


class RemoteExtractorResult(BaseModel):
    extracted: ExtractedTenderV1

    model_config = ConfigDict(extra="ignore")
