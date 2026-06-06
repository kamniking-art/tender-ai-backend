"""Tender Opportunity Report — pure Pydantic models, no IO."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OpportunityReport(BaseModel):
    """Deterministic analysis of a tender opportunity.

    Generated from already-computed data: fit score components, risk score,
    risk_flags, extracted requirements and deadline. No LLM involved.
    """
    strengths: list[str] = Field(default_factory=list)
    """What matched: OKVED, region, NMCK range, etc."""

    risks: list[str] = Field(default_factory=list)
    """What is wrong: profile mismatches, high risk flags, penalties."""

    missing_information: list[str] = Field(default_factory=list)
    """Data unavailable to make a confident decision."""

    required_documents: list[str] = Field(default_factory=list)
    """Documents / requirements extracted from tender text."""

    recommended_actions: list[str] = Field(default_factory=list)
    """What to do next based on recommendation and gaps."""

    recommendation: str = "unsure"
    """Copied from TenderDecision for quick access."""

    score: int | None = None
    """Copied decision_score."""

    generated_at: datetime = Field(default_factory=datetime.utcnow)
