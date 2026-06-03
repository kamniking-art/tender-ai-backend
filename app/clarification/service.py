from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.clarification.schema import (
    ClarificationStateError,  # noqa: F401 — re-exported for callers
    check_clarification_transition,
)

# ── Enum ──────────────────────────────────────────────────────────────────────

_CLARIFICATION_STATUS_ENUM = sa.Enum(
    "draft", "approved", "sent", "answered", "timeout",
    name="clarification_status_enum",
    create_type=False,  # already exists in DB (migration 20260520_18)
)

# ── ORM model ─────────────────────────────────────────────────────────────────


class ClarificationQuestion(Base):
    __tablename__ = "clarification_questions"

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
    question_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    status: Mapped[str] = mapped_column(
        _CLARIFICATION_STATUS_ENUM, nullable=False, server_default="draft"
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    answer_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    timeout_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    telegram_message_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def create_question(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    question_text: str,
    reason: str | None = None,
    timeout_at: datetime | None = None,
) -> ClarificationQuestion:
    """Create a clarification question in 'draft' status."""
    now = datetime.now(timezone.utc)
    q = ClarificationQuestion(
        id=uuid.uuid4(),
        company_id=company_id,
        tender_id=tender_id,
        question_text=question_text,
        reason=reason,
        status="draft",
        timeout_at=timeout_at,
        created_at=now,
        updated_at=now,
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


async def _get_question(db: AsyncSession, question_id: UUID) -> ClarificationQuestion:
    q = await db.scalar(
        select(ClarificationQuestion).where(ClarificationQuestion.id == question_id)
    )
    if q is None:
        raise ValueError(f"ClarificationQuestion {question_id} not found")
    return q


async def approve_question(
    db: AsyncSession,
    question_id: UUID,
) -> ClarificationQuestion:
    """Approve a draft question (draft → approved)."""
    q = await _get_question(db, question_id)
    check_clarification_transition(q.status, "approve")
    q.status = "approved"
    q.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(q)
    return q


async def mark_sent(
    db: AsyncSession,
    question_id: UUID,
) -> ClarificationQuestion:
    """Mark a question as sent (approved → sent). Only valid after approval."""
    q = await _get_question(db, question_id)
    check_clarification_transition(q.status, "send")
    now = datetime.now(timezone.utc)
    q.status = "sent"
    q.sent_at = now
    q.updated_at = now
    await db.commit()
    await db.refresh(q)
    return q


async def record_answer(
    db: AsyncSession,
    question_id: UUID,
    answer_text: str,
) -> ClarificationQuestion:
    """Record a received answer (sent → answered)."""
    q = await _get_question(db, question_id)
    check_clarification_transition(q.status, "answer")
    now = datetime.now(timezone.utc)
    q.status = "answered"
    q.answer_text = answer_text
    q.answered_at = now
    q.updated_at = now
    await db.commit()
    await db.refresh(q)
    return q


async def timeout_question(
    db: AsyncSession,
    question_id: UUID,
) -> ClarificationQuestion:
    """Mark a question as timed out."""
    q = await _get_question(db, question_id)
    check_clarification_transition(q.status, "timeout")
    q.status = "timeout"
    q.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(q)
    return q


async def list_questions(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
) -> list[ClarificationQuestion]:
    """Return all questions for a tender, newest first."""
    result = await db.scalars(
        select(ClarificationQuestion)
        .where(
            ClarificationQuestion.company_id == company_id,
            ClarificationQuestion.tender_id == tender_id,
        )
        .order_by(ClarificationQuestion.created_at.desc())
    )
    return list(result.all())
