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
from app.fit_score.schema import FitScoreResult

# ── ORM model ─────────────────────────────────────────────────────────────────


class CompanyFitScore(Base):
    __tablename__ = "company_fit_score"

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
    okved_match: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    sro_ok: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    license_ok: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    experience_ok: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    funds_ok: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
    fit_score: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


# ── Upsert ────────────────────────────────────────────────────────────────────


async def upsert_fit_score(
    db: AsyncSession,
    tender_id: UUID,
    company_id: UUID,
    result: FitScoreResult,
    extracted_at: Optional[datetime] = None,  # tracking only — no DB column
) -> CompanyFitScore:
    """Idempotent upsert of a FitScoreResult.

    Uses unique constraint (company_id, tender_id) — safe to call multiple times.

    extracted_at is accepted for caller tracking but not persisted
    (company_fit_score has no extracted_at column).
    """
    now = datetime.now(timezone.utc)
    c = result.components

    existing = await db.scalar(
        select(CompanyFitScore).where(
            CompanyFitScore.company_id == company_id,
            CompanyFitScore.tender_id == tender_id,
        )
    )

    if existing is not None:
        existing.okved_match   = c.okved
        existing.sro_ok        = c.sro
        existing.license_ok    = c.license
        existing.experience_ok = c.experience
        existing.funds_ok      = c.finance
        existing.fit_score     = result.fit_score
        existing.updated_at    = now
        await db.commit()
        await db.refresh(existing)
        return existing

    record = CompanyFitScore(
        id=uuid.uuid4(),
        company_id=company_id,
        tender_id=tender_id,
        okved_match=c.okved,
        sro_ok=c.sro,
        license_ok=c.license,
        experience_ok=c.experience,
        funds_ok=c.finance,
        fit_score=result.fit_score,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record
