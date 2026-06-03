import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Tender(Base):
    __tablename__ = "tenders"
    __table_args__ = (
        UniqueConstraint("company_id", "source", "external_id", name="uq_tenders_company_source_external"),
        Index("idx_tenders_company_status", "company_id", "status"),
        Index("idx_tenders_company_deadline", "company_id", "submission_deadline"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_email: Mapped[str | None] = mapped_column(Text, nullable=True)
    region: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    place_text: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    procurement_type: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    nmck: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True, index=True)
    nmck_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    nmck_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 2), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    submission_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deadline_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    deadline_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    deadline_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new", server_default="new", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
