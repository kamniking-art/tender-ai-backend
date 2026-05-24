"""Fit score models.

No IO, no DB, no SQLAlchemy — safe to import in pure-unit-test environments.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class FitScoreComponents(BaseModel):
    """Per-component match results.  None = data unavailable (neutral)."""
    okved: bool | None
    sro: bool | None
    license: bool | None
    experience: bool | None
    finance: bool | None


class FitScoreResult(BaseModel):
    """Scorer output: component flags + weighted aggregate score."""
    components: FitScoreComponents
    fit_score: float = Field(ge=0.0, le=100.0)
    # Tracking field — not persisted to DB (company_fit_score has no extracted_at column)
    extracted_at: datetime | None = None
