import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TenderDecision(Base):
    __tablename__ = "tender_decisions"
    __table_args__ = (
        UniqueConstraint("company_id", "tender_id", name="uq_tender_decisions_company_tender"),
        Index("idx_tender_decisions_company_recommendation", "company_id", "recommendation"),
        Index("idx_tender_decisions_company_risk_score", "company_id", "risk_score"),
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
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="unsure", server_default="unsure")
    rationale: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    assumptions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))

    nmck: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_revenue: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    cogs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    logistics_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    other_costs: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_margin_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    expected_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)

    risk_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    risk_flags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    decision_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recommendation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine_meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))

    need_bid_security: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    bid_security_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    need_contract_security: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    contract_security_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
