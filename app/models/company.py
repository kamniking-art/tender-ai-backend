import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ogrn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    legal_address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bank_details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    scoring_settings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    finance_settings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    users: Mapped[list["User"]] = relationship("User", back_populates="company", cascade="all, delete-orphan")
