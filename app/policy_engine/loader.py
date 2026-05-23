from __future__ import annotations

import logging
import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.policy_engine.schema import PolicySchema
from app.policy_engine.validator import PolicyValidator

logger = logging.getLogger(__name__)


class Policy(Base):
    """ORM projection of the policies table. Read-only from this module."""

    __tablename__ = "policies"

    policy_id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    policy_type: Mapped[str] = mapped_column(Text, nullable=False)
    condition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    action: Mapped[dict] = mapped_column(JSONB, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PolicyLoader:
    """Reads active policies from the DB, validates each, returns usable list.

    Invalid policies are logged and excluded — never raise.
    """

    def __init__(self) -> None:
        self._validator = PolicyValidator()

    async def load(self, db: AsyncSession, company_id: UUID) -> list[PolicySchema]:
        rows = await db.scalars(
            select(Policy)
            .where(Policy.company_id == company_id, Policy.active.is_(True))
            .order_by(Policy.priority.desc(), Policy.created_at.asc())
        )

        valid: list[PolicySchema] = []
        for row in rows:
            raw = {
                "policy_id": row.policy_id,
                "company_id": row.company_id,
                "policy_type": row.policy_type,
                "condition": row.condition,
                "action": row.action,
                "priority": row.priority,
                "active": row.active,
                "created_at": row.created_at,
            }
            schema = self._validator.validate(raw)
            if schema is not None:
                valid.append(schema)
            else:
                logger.error(
                    "Policy %s for company %s is invalid and will not be applied",
                    row.policy_id,
                    company_id,
                )

        logger.debug("Loaded %d valid policies for company %s", len(valid), company_id)
        return valid
