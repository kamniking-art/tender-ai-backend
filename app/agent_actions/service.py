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

# ── Constants ─────────────────────────────────────────────────────────────────

SYSTEM_AGENT_ID = UUID("b4742800-bbbc-48ae-8853-f23c6b0cf564")

# ── Enum ──────────────────────────────────────────────────────────────────────

_ACTION_STATUS_ENUM = sa.Enum(
    "pending", "running", "completed", "failed", "rolled_back",
    name="action_status_enum",
    create_type=False,  # already exists in DB
)

# ── ORM model ─────────────────────────────────────────────────────────────────


class AgentAction(Base):
    __tablename__ = "actions"

    action_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        sa.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        _ACTION_STATUS_ENUM, nullable=False, server_default="pending"
    )
    confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    rollback_possible: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def create_action(
    db: AsyncSession,
    *,
    company_id: UUID,
    agent_id: UUID,
    action_type: str,
    target: str | None = None,
    payload: dict | None = None,
    task_id: UUID | None = None,
    confidence: float | None = None,
    rollback_possible: bool = False,
) -> AgentAction:
    """Create an action record with deduplication.

    Before inserting, checks for an existing record with the same
    (company_id, action_type, target) in pending/running/completed state.
    If found, returns the existing record instead of creating a duplicate.
    Failed records are always retried (a new record is created).
    """
    existing = await db.scalar(
        select(AgentAction).where(
            AgentAction.company_id == company_id,
            AgentAction.action_type == action_type,
            AgentAction.target == target,
            AgentAction.status.in_(["pending", "running", "completed"]),
        )
    )
    if existing is not None:
        return existing

    now = datetime.now(timezone.utc)
    action = AgentAction(
        action_id=uuid.uuid4(),
        company_id=company_id,
        agent_id=agent_id,
        task_id=task_id,
        action_type=action_type,
        target=target,
        payload=payload or {},
        status="running",
        confidence=confidence,
        rollback_possible=rollback_possible,
        result=None,
        created_at=now,
        updated_at=now,
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


async def complete_action(
    db: AsyncSession,
    action_id: UUID,
    result: dict | None = None,
) -> AgentAction:
    """Mark an action as completed with an optional result payload."""
    action = await db.scalar(
        select(AgentAction).where(AgentAction.action_id == action_id)
    )
    if action is None:
        raise ValueError(f"Action {action_id} not found")
    action.status = "completed"
    action.result = result
    action.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(action)
    return action


async def fail_action(
    db: AsyncSession,
    action_id: UUID,
    result: dict | None = None,
) -> AgentAction:
    """Mark an action as failed with an optional error result payload."""
    action = await db.scalar(
        select(AgentAction).where(AgentAction.action_id == action_id)
    )
    if action is None:
        raise ValueError(f"Action {action_id} not found")
    action.status = "failed"
    action.result = result
    action.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(action)
    return action
