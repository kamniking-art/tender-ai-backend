from __future__ import annotations

import uuid
from datetime import datetime, timezone
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.escalation.schema import (
    ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    EscalationStateError,
    check_transition,
)

# ── Enum ──────────────────────────────────────────────────────────────────────

_ESCALATION_STATUS_ENUM = sa.Enum(
    "pending", "approved", "rejected", "timeout", "reminded",
    name="escalation_status_enum",
    create_type=False,  # already exists in DB (reminded added in migration 19)
)

# ── ORM model ─────────────────────────────────────────────────────────────────


class Escalation(Base):
    __tablename__ = "escalations"

    escalation_id: Mapped[uuid.UUID] = mapped_column(
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
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    status: Mapped[str] = mapped_column(
        _ESCALATION_STATUS_ENUM, nullable=False, server_default="pending"
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    override_note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Added in migration 20260522_19:
    escalation_type: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    telegram_message_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Added in migration 20260522_20:
    tender_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("tenders.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def get_active_escalation(
    db: AsyncSession,
    *,
    company_id: UUID,
    escalation_type: str,
    tender_id: UUID | None = None,
) -> Escalation | None:
    """Return the active (pending/reminded) escalation for this scope, or None."""
    stmt = (
        select(Escalation)
        .where(
            Escalation.company_id == company_id,
            Escalation.escalation_type == escalation_type,
            Escalation.status.in_(list(ACTIVE_STATUSES)),
        )
        .order_by(Escalation.created_at.desc())
        .limit(1)
    )
    if tender_id is not None:
        stmt = stmt.where(Escalation.tender_id == tender_id)
    else:
        stmt = stmt.where(Escalation.tender_id.is_(None))
    return await db.scalar(stmt)


async def create_escalation(
    db: AsyncSession,
    *,
    company_id: UUID,
    escalation_type: str,
    reason: str,
    agent_id: UUID | None = None,
    tender_id: UUID | None = None,
    confidence: float | None = None,
    extra_payload: dict | None = None,
) -> Escalation:
    """Create an escalation with idempotency.

    Before inserting, checks for an existing active (pending/reminded) record
    with the same (company_id, escalation_type, tender_id).
    If found, returns the existing record — no duplicate is created.
    """
    existing = await get_active_escalation(
        db,
        company_id=company_id,
        escalation_type=escalation_type,
        tender_id=tender_id,
    )
    if existing is not None:
        return existing

    full_payload: dict = dict(extra_payload or {})
    if tender_id is not None:
        # Keep tender_id in payload for backward-compat with existing queries,
        # but now also write to the dedicated column (migration 20260522_20).
        full_payload["tender_id"] = str(tender_id)

    now = datetime.now(timezone.utc)
    esc = Escalation(
        escalation_id=uuid.uuid4(),
        company_id=company_id,
        agent_id=agent_id,
        escalation_type=escalation_type,
        reason=reason,
        confidence=confidence,
        status="pending",
        tender_id=tender_id,
        payload=full_payload,
        created_at=now,
        updated_at=now,
    )
    db.add(esc)
    await db.commit()
    await db.refresh(esc)
    return esc


async def approve_escalation(
    db: AsyncSession,
    escalation_id: UUID,
    *,
    approved_by: UUID | None = None,
    override_note: str | None = None,
) -> Escalation:
    """Approve an escalation.

    Idempotent — calling on an already-approved escalation is a no-op.
    Raises EscalationStateError if the escalation is in a different terminal state.
    """
    esc = await db.scalar(
        select(Escalation).where(Escalation.escalation_id == escalation_id)
    )
    if esc is None:
        raise ValueError(f"Escalation {escalation_id} not found")

    if not check_transition(esc.status, "approved"):
        return esc  # already approved — no-op

    now = datetime.now(timezone.utc)
    esc.status = "approved"
    esc.approved_by = approved_by
    esc.approved_at = now
    esc.override_note = override_note
    esc.updated_at = now
    await db.commit()
    await db.refresh(esc)
    return esc


async def reject_escalation(
    db: AsyncSession,
    escalation_id: UUID,
    *,
    override_note: str | None = None,
) -> Escalation:
    """Reject an escalation.

    Idempotent — calling on an already-rejected escalation is a no-op.
    Raises EscalationStateError if the escalation is in a different terminal state.
    """
    esc = await db.scalar(
        select(Escalation).where(Escalation.escalation_id == escalation_id)
    )
    if esc is None:
        raise ValueError(f"Escalation {escalation_id} not found")

    if not check_transition(esc.status, "rejected"):
        return esc  # already rejected — no-op

    now = datetime.now(timezone.utc)
    esc.status = "rejected"
    esc.override_note = override_note
    esc.updated_at = now
    await db.commit()
    await db.refresh(esc)
    return esc


async def timeout_escalation(
    db: AsyncSession,
    escalation_id: UUID,
) -> Escalation:
    """Mark an escalation as timed out.

    Idempotent — calling on an already-timed-out escalation is a no-op.
    Raises EscalationStateError if the escalation is in a different terminal state.
    """
    esc = await db.scalar(
        select(Escalation).where(Escalation.escalation_id == escalation_id)
    )
    if esc is None:
        raise ValueError(f"Escalation {escalation_id} not found")

    if not check_transition(esc.status, "timeout"):
        return esc  # already timed out — no-op

    esc.status = "timeout"
    esc.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(esc)
    return esc
