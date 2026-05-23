"""Canonical requirement types and normalised requirement model.

No IO, no DB, no SQLAlchemy — safe to import in pure-unit-test environments.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel


class RequirementType(StrEnum):
    BID_SECURITY = "bid_security"
    CONTRACT_SECURITY = "contract_security"
    SRO = "sro"
    LICENSE = "license"
    EXPERIENCE = "experience"
    BANK_GUARANTEE = "bank_guarantee"
    EXECUTION_TIMELINE = "execution_timeline"


# Ordered list used by normalizer to guarantee all 7 types are always returned.
ALL_REQUIREMENT_TYPES: list[RequirementType] = list(RequirementType)


class NormalizedRequirement(BaseModel):
    canonical_type: RequirementType
    required: bool
    status: str                     # ok | missing | unknown | risk
    evidence: str | None = None
    # Optional enrichment fields — not persisted to DB, used for tracking/display
    amount: Decimal | None = None   # monetary amount if known (e.g. bid_security_amount)
    percent: Decimal | None = None  # percentage if known (e.g. bid_security_pct)
    raw_text: str | None = None     # original text fragment from the document
    confidence: float | None = None  # extraction confidence score (0–1)
