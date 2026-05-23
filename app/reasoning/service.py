from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.reasoning.schema import map_recommendation_to_decision  # re-export


# ── ORM model ─────────────────────────────────────────────────────────────────


class ReasoningTrace(Base):
    __tablename__ = "reasoning_traces"

    trace_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    tender_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    decision: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    factors: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rules_fired: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    evidence_used: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── Create ────────────────────────────────────────────────────────────────────


async def create_trace(
    db: AsyncSession,
    *,
    company_id: UUID,
    recommendation: str | None,
    factors: list | None = None,
    rules_fired: list | None = None,
    evidence_used: list | None = None,
    confidence: float | int | None = None,
    tender_id: UUID | None = None,
    agent_id: UUID | None = None,
) -> ReasoningTrace:
    """Persist a reasoning trace for a decision-engine run.

    Args:
        company_id:    company scope.
        recommendation: raw engine recommendation string (e.g. "strong_go").
                        Mapped to canonical decision label via map_recommendation_to_decision().
        factors:        list of explanation strings from engine["explain"].
        rules_fired:    list of rule identifiers that were triggered.
        evidence_used:  list of evidence items that informed the decision.
        confidence:     numeric confidence / decision_score (0–100).
        tender_id:      optional tender scope.
        agent_id:       optional agent that generated the trace.
    """
    now = datetime.now(timezone.utc)
    trace = ReasoningTrace(
        trace_id=uuid.uuid4(),
        company_id=company_id,
        agent_id=agent_id,
        tender_id=tender_id,
        decision=map_recommendation_to_decision(recommendation),
        factors=factors or [],
        rules_fired=rules_fired or [],
        evidence_used=evidence_used or [],
        confidence=float(confidence) if confidence is not None else None,
        created_at=now,
    )
    db.add(trace)
    await db.commit()
    await db.refresh(trace)
    return trace
