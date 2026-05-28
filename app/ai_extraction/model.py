import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AICostLog(Base):
    __tablename__ = "ai_cost_log"
    __table_args__ = (
        Index("idx_ai_cost_log_company_tender_created", "company_id", "tender_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    model: Mapped[str] = mapped_column(Text, nullable=False)
    operation_type: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="extraction | fallback | cache_hit")
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    chars_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(18, 8), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="ok | error | timeout")
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class ExtractionEvidence(Base):
    """Per-field extraction evidence and confidence scores.

    One row per (company, tender, field). Re-extraction uses ON CONFLICT DO UPDATE
    so the row is always current without duplicates.
    """

    __tablename__ = "extraction_evidence"
    __table_args__ = (
        UniqueConstraint("company_id", "tender_id", "field_name", name="uq_extraction_evidence_company_tender_field"),
        Index("idx_extraction_evidence_company_tender", "company_id", "tender_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extraction_completeness: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExtractionSnapshot(Base):
    """Immutable append-only history of every successful extraction run.

    Never UPDATE — each rerun inserts a new row. Enables re-score and
    re-debug without repeating the AI call.
    """

    __tablename__ = "extraction_snapshots"
    __table_args__ = (
        Index("idx_extraction_snapshots_company_tender", "company_id", "tender_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    extracted_v1: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    extract_meta_v1: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pipeline_versions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    doc_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)

