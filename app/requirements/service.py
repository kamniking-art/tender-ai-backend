from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.requirements.schema import NormalizedRequirement

# ── ORM model ─────────────────────────────────────────────────────────────────

_REQUIREMENT_STATUS_ENUM = sa.Enum(
    "ok", "missing", "unknown", "risk",
    name="requirement_status_enum",
    create_type=False,  # already exists in DB
)


class RequirementsChecklist(Base):
    __tablename__ = "requirements_checklist"

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
    requirement_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    required: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    status: Mapped[str] = mapped_column(
        _REQUIREMENT_STATUS_ENUM, nullable=False, server_default="unknown"
    )
    evidence: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── Upsert ────────────────────────────────────────────────────────────────────

async def upsert_checklist(
    db: AsyncSession,
    tender_id: UUID,
    company_id: UUID,
    requirements: list[NormalizedRequirement],
    extracted_at: Optional[datetime] = None,  # tracking only — no DB column
) -> None:
    """Idempotent upsert for all NormalizedRequirement items.

    Uses the unique constraint (company_id, tender_id, requirement_type) —
    safe to call multiple times without duplicates.

    extracted_at is accepted for caller tracking but not persisted
    (requirements_checklist has no extracted_at column).
    """
    now = datetime.now(timezone.utc)

    for req in requirements:
        existing = await db.scalar(
            select(RequirementsChecklist).where(
                RequirementsChecklist.company_id == company_id,
                RequirementsChecklist.tender_id == tender_id,
                RequirementsChecklist.requirement_type == req.canonical_type,
            )
        )

        if existing is not None:
            existing.required = req.required
            existing.status = req.status
            existing.evidence = req.evidence
            existing.updated_at = now
        else:
            db.add(
                RequirementsChecklist(
                    id=uuid.uuid4(),
                    company_id=company_id,
                    tender_id=tender_id,
                    requirement_type=str(req.canonical_type),
                    required=req.required,
                    status=req.status,
                    evidence=req.evidence,
                    created_at=now,
                    updated_at=now,
                )
            )

    await db.commit()
