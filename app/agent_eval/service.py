from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.agent_eval.schema import compute_was_right

# ── Enums ─────────────────────────────────────────────────────────────────────

_AGENT_RECOMMENDATION_ENUM = sa.Enum(
    "go", "no_go", "needs_review",
    name="agent_recommendation_enum",
    create_type=False,  # created in migration 20260522_22
)

_HUMAN_DECISION_ENUM = sa.Enum(
    "participate", "skip", "deferred",
    name="human_decision_enum",
    create_type=False,  # created in migration 20260522_22
)

_ACTUAL_RESULT_ENUM = sa.Enum(
    "won", "lost", "cancelled", "not_submitted",
    name="actual_result_enum",
    create_type=False,  # created in migration 20260522_22
)


# ── ORM model ─────────────────────────────────────────────────────────────────


class AgentEvaluation(Base):
    __tablename__ = "agent_evaluation"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_recommendation: Mapped[str | None] = mapped_column(
        _AGENT_RECOMMENDATION_ENUM, nullable=True
    )
    human_decision: Mapped[str | None] = mapped_column(
        _HUMAN_DECISION_ENUM, nullable=True
    )
    actual_result: Mapped[str | None] = mapped_column(
        _ACTUAL_RESULT_ENUM, nullable=True
    )
    was_right: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def upsert_evaluation(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    agent_recommendation: str | None = None,
    human_decision: str | None = None,
    actual_result: str | None = None,
    notes: str | None = None,
) -> AgentEvaluation:
    """Create or update an evaluation record for a tender.

    Automatically computes ``was_right`` from recommendation + decision + result.
    One record per (company_id, tender_id) — subsequent calls update in place.
    """
    evaluation = await db.scalar(
        select(AgentEvaluation).where(
            AgentEvaluation.company_id == company_id,
            AgentEvaluation.tender_id == tender_id,
        )
    )

    now = datetime.now(timezone.utc)

    if evaluation is None:
        evaluation = AgentEvaluation(
            id=uuid.uuid4(),
            company_id=company_id,
            tender_id=tender_id,
            created_at=now,
        )
        db.add(evaluation)

    if agent_recommendation is not None:
        evaluation.agent_recommendation = agent_recommendation
    if human_decision is not None:
        evaluation.human_decision = human_decision
    if actual_result is not None:
        evaluation.actual_result = actual_result
    if notes is not None:
        evaluation.notes = notes

    # Recompute was_right whenever we have all three pieces
    evaluation.was_right = compute_was_right(
        evaluation.agent_recommendation,
        evaluation.human_decision,
        evaluation.actual_result,
    )

    # Mark evaluated_at when the record first becomes conclusive
    if evaluation.was_right is not None and evaluation.evaluated_at is None:
        evaluation.evaluated_at = now

    evaluation.updated_at = now

    await db.commit()
    await db.refresh(evaluation)
    return evaluation


async def get_evaluation(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
) -> AgentEvaluation | None:
    """Return the evaluation record for a tender, or None."""
    return await db.scalar(
        select(AgentEvaluation).where(
            AgentEvaluation.company_id == company_id,
            AgentEvaluation.tender_id == tender_id,
        )
    )


async def get_evaluation_stats(
    db: AsyncSession,
    *,
    company_id: UUID,
) -> dict:
    """Return aggregate accuracy statistics for a company.

    Returns:
        {
            "total": int,           # all evaluations
            "conclusive": int,      # where was_right IS NOT NULL
            "correct": int,         # where was_right = True
            "incorrect": int,       # where was_right = False
            "accuracy_pct": float | None,  # correct / conclusive * 100
        }
    """
    total_row = await db.scalar(
        select(func.count()).where(AgentEvaluation.company_id == company_id)
    )
    total = total_row or 0

    correct_row = await db.scalar(
        select(func.count()).where(
            AgentEvaluation.company_id == company_id,
            AgentEvaluation.was_right.is_(True),
        )
    )
    correct = correct_row or 0

    incorrect_row = await db.scalar(
        select(func.count()).where(
            AgentEvaluation.company_id == company_id,
            AgentEvaluation.was_right.is_(False),
        )
    )
    incorrect = incorrect_row or 0

    conclusive = correct + incorrect
    accuracy_pct = round(correct / conclusive * 100, 1) if conclusive > 0 else None

    return {
        "total": total,
        "conclusive": conclusive,
        "correct": correct,
        "incorrect": incorrect,
        "accuracy_pct": accuracy_pct,
    }
