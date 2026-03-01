from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_finance.schemas import TenderFinanceRead, TenderFinanceUpsert
from app.tender_finance.service import ScopedNotFoundError, ensure_tender_scoped, get_finance_scoped, upsert_finance

router = APIRouter(prefix="/tenders/{tender_id}/finance", tags=["tender-finance"])


@router.get("", response_model=TenderFinanceRead)
async def get_finance_endpoint(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderFinanceRead:
    try:
        await ensure_tender_scoped(db, current_user.company_id, tender_id)
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    finance = await get_finance_scoped(db, current_user.company_id, tender_id)
    if finance is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finance not found")

    return TenderFinanceRead.model_validate(finance)


@router.put("", response_model=TenderFinanceRead)
async def upsert_finance_endpoint(
    tender_id: UUID,
    payload: TenderFinanceUpsert,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderFinanceRead:
    try:
        finance = await upsert_finance(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            payload=payload,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return TenderFinanceRead.model_validate(finance)
