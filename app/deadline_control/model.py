from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Reuse the enum that already exists in the DB (create_type=False → never DDL)
_DEADLINE_STATUS_ENUM = sa.Enum(
    "safe",
    "warning",
    "urgent",
    "expired",
    name="deadline_status_enum",
    create_type=False,
)


class DeadlineControl(Base):
    __tablename__ = "deadline_control"

    tender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenders.id", ondelete="CASCADE"),
        primary_key=True,
    )
    company_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("companies.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    submission_deadline: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    hours_remaining: Mapped[int | None] = mapped_column(
        sa.Integer,
        nullable=True,
        comment="Derived field, updated by service/scheduler",
    )
    deadline_status: Mapped[str] = mapped_column(
        _DEADLINE_STATUS_ENUM,
        nullable=False,
        server_default="safe",
    )
    can_recommend_go: Mapped[bool] = mapped_column(
        sa.Boolean,
        nullable=False,
        server_default=sa.text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
