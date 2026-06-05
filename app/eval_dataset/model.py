from uuid import UUID
from sqlalchemy import Text, DateTime, UniqueConstraint, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from app.models.base import Base


class TenderEvalDataset(Base):
    __tablename__ = "tender_eval_dataset"
    __table_args__ = (
        UniqueConstraint("tender_id", "company_id", name="uq_eval_dataset_tender_company"),
        Index("ix_eval_dataset_company_id", "company_id"),
        Index("ix_eval_dataset_expected_decision", "expected_decision"),
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tender_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), ForeignKey("tenders.id"), nullable=False)
    company_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), ForeignKey("companies.id"), nullable=True)
    expected_decision: Mapped[str] = mapped_column(Text, nullable=False)  # go/no_go/review
    expected_risks: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    expected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
