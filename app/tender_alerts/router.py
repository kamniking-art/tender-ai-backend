from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_alerts.schemas import AlertAckRequest, AlertAckResponse, AlertCategory, AlertDigestResponse, AlertSummaryResponse
from app.tender_alerts.service import ack_alert, build_alert_digest, ensure_tender_scoped

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/tenders", response_model=AlertDigestResponse)
async def get_tender_alerts(
    since: datetime | None = None,
    include_acknowledged: bool = False,
    categories: list[AlertCategory] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlertDigestResponse:
    return await build_alert_digest(
        db=db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        since=since,
        include_acknowledged=include_acknowledged,
        categories=categories,
    )


@router.get("/summary", response_model=AlertSummaryResponse)
async def get_alerts_summary(
    since: datetime | None = None,
    include_acknowledged: bool = False,
    categories: list[AlertCategory] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlertSummaryResponse:
    digest = await build_alert_digest(
        db=db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        since=since,
        include_acknowledged=include_acknowledged,
        categories=categories,
        limit=1,
    )
    return AlertSummaryResponse(counts=digest.counts)


@router.post("/tenders/{tender_id}/ack", response_model=AlertAckResponse)
async def acknowledge_alert(
    tender_id: UUID,
    payload: AlertAckRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> AlertAckResponse:
    scoped = await ensure_tender_scoped(db, current_user.company_id, tender_id)
    if not scoped:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    await ack_alert(
        db=db,
        company_id=current_user.company_id,
        user_id=current_user.id,
        tender_id=tender_id,
        category=payload.category,
    )
    return AlertAckResponse(ok=True)
