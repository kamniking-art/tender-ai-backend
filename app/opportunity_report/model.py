"""ORM model for TenderOpportunityReport."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TenderOpportunityReport(Base):
    __tablename__ = "tender_opportunity_report"
    __table_args__ = (
        sa.UniqueConstraint("tender_id", "company_id", name="uq_opp_report_tender_company"),
        sa.Index("ix_opp_report_company_id", "company_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tender_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("tenders.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    strengths: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="'[]'::jsonb")
    risks: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="'[]'::jsonb")
    missing_information: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="'[]'::jsonb")
    required_documents: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="'[]'::jsonb")
    recommended_actions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default="'[]'::jsonb")
    recommendation: Mapped[str] = mapped_column(sa.Text, nullable=False, default="unsure")
    score: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
    )


async def upsert_report(db, tender_id, company_id, report) -> TenderOpportunityReport:
    """Create or update the opportunity report for a tender."""
    from sqlalchemy import select
    from sqlalchemy.orm import attributes

    now = datetime.now(timezone.utc)
    existing = await db.scalar(
        select(TenderOpportunityReport).where(
            TenderOpportunityReport.tender_id == tender_id,
            TenderOpportunityReport.company_id == company_id,
        )
    )
    if existing:
        existing.strengths = report.strengths
        existing.risks = report.risks
        existing.missing_information = report.missing_information
        existing.required_documents = report.required_documents
        existing.recommended_actions = report.recommended_actions
        existing.recommendation = report.recommendation
        existing.score = report.score
        existing.generated_at = now
        existing.updated_at = now
        for field in ("strengths", "risks", "missing_information", "required_documents", "recommended_actions"):
            attributes.flag_modified(existing, field)
        await db.commit()
        await db.refresh(existing)
        return existing

    record = TenderOpportunityReport(
        id=uuid.uuid4(),
        tender_id=tender_id,
        company_id=company_id,
        strengths=report.strengths,
        risks=report.risks,
        missing_information=report.missing_information,
        required_documents=report.required_documents,
        recommended_actions=report.recommended_actions,
        recommendation=report.recommendation,
        score=report.score,
        generated_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record
