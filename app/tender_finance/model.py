import uuid
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Numeric, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TenderFinance(Base):
    __tablename__ = "tender_finance"
    __table_args__ = (
        UniqueConstraint("company_id", "tender_id", name="uq_tender_finance_company_tender"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    participation_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    win_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Financial snapshot output fields — added in migration 20260522_20:
    profitability_status: Mapped[str | None] = mapped_column(
        sa.Enum("go", "no_go", "requires_analysis",
                name="profitability_status_enum", create_type=False),
        nullable=True,
    )
    is_loss_leader: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    gross_margin: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    gross_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4), nullable=True)
    expected_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    finance_calculated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Snapshot input fields — added in migration 20260522_21:
    snapshot_contract_value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    snapshot_cost_estimate: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    snapshot_participation_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    snapshot_win_probability: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
