from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tenders.schemas import (
    SortField,
    SortOrder,
    TenderCreate,
    TenderListResponse,
    TenderRead,
    TenderStatus,
    TenderStatusUpdate,
    TenderUpdate,
)
from app.tenders.service import (
    create_tender,
    get_tender_by_id_scoped,
    list_tenders,
    set_tender_status,
    update_tender,
)
from app.tender_decisions.service import get_decision_scoped

router = APIRouter(prefix="/tenders", tags=["tenders"])


async def _to_tender_read(db: AsyncSession, company_id: UUID, tender) -> TenderRead:
    payload = TenderRead.model_validate(tender)
    decision = await get_decision_scoped(db, company_id, tender.id)
    if decision is None:
        return payload
    return payload.model_copy(
        update={
            "decision_score": decision.decision_score,
            "recommendation": decision.recommendation,
            "recommendation_reason": decision.recommendation_reason,
        }
    )


@router.post("", response_model=TenderRead, status_code=status.HTTP_201_CREATED)
async def create_tender_endpoint(
    payload: TenderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderRead:
    try:
        tender = await create_tender(db, current_user.company_id, payload)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tender already exists")

    return await _to_tender_read(db, current_user.company_id, tender)


@router.get("", response_model=TenderListResponse)
async def list_tenders_endpoint(
    status: TenderStatus | None = None,
    region: str | None = None,
    procurement_type: str | None = None,
    nmck_min: Decimal | None = None,
    nmck_max: Decimal | None = None,
    deadline_from: datetime | None = None,
    deadline_to: datetime | None = None,
    q: str | None = None,
    sort: SortField | None = None,
    order: SortOrder = SortOrder.ASC,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderListResponse:
    items, total = await list_tenders(
        db,
        company_id=current_user.company_id,
        status=status,
        region=region,
        procurement_type=procurement_type,
        nmck_min=nmck_min,
        nmck_max=nmck_max,
        deadline_from=deadline_from,
        deadline_to=deadline_to,
        q=q,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    payload: list[TenderRead] = []
    for item in items:
        payload.append(await _to_tender_read(db, current_user.company_id, item))
    return TenderListResponse(items=payload, total=total, limit=limit, offset=offset)


@router.get("/{tender_id}", response_model=TenderRead)
async def get_tender_endpoint(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderRead:
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")
    return await _to_tender_read(db, current_user.company_id, tender)


@router.patch("/{tender_id}", response_model=TenderRead)
async def update_tender_endpoint(
    tender_id: UUID,
    payload: TenderUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderRead:
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    updated = await update_tender(db, tender, payload)
    return await _to_tender_read(db, current_user.company_id, updated)


@router.patch("/{tender_id}/status", response_model=TenderRead)
async def set_tender_status_endpoint(
    tender_id: UUID,
    payload: TenderStatusUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderRead:
    tender = await get_tender_by_id_scoped(db, current_user.company_id, tender_id)
    if tender is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    updated = await set_tender_status(db, tender, payload.status)
    return await _to_tender_read(db, current_user.company_id, updated)
