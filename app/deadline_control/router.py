from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.deadline_control.model import DeadlineControl
from app.deadline_control.service import refresh_all
from app.models import User

router = APIRouter(prefix="/deadline-control", tags=["deadline-control"])


class DeadlineControlResponse(BaseModel):
    tender_id: UUID
    company_id: UUID
    submission_deadline: datetime | None
    hours_remaining: int | None
    deadline_status: str
    can_recommend_go: bool
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RefreshResponse(BaseModel):
    updated: int


@router.get("/tenders/{tender_id}", response_model=DeadlineControlResponse)
async def get_deadline_status(
    tender_id: UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeadlineControlResponse:
    """Return the current deadline control record for a tender."""
    record = await db.scalar(
        select(DeadlineControl).where(
            DeadlineControl.tender_id == tender_id,
            DeadlineControl.company_id == current_user.company_id,
        )
    )
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Deadline control record not found",
        )
    return DeadlineControlResponse.model_validate(record)


@router.post("/refresh", response_model=RefreshResponse)
async def refresh_deadline_control(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RefreshResponse:
    """Recalculate deadline_control for all active tenders of the current company."""
    updated = await refresh_all(db, current_user.company_id)
    return RefreshResponse(updated=updated)
