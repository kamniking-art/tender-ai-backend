from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models import User
from app.tender_analysis.schemas import TenderAnalysisCreate, TenderAnalysisPatch, TenderAnalysisRead
from app.tender_analysis.service import (
    AnalysisConflictError,
    ScopedNotFoundError,
    approve_analysis,
    create_or_update_analysis,
    ensure_tender_scoped,
    get_analysis_scoped,
    patch_analysis,
)

router = APIRouter(prefix="/tenders/{tender_id}/analysis", tags=["tender-analysis"])


@router.post("", response_model=TenderAnalysisRead)
async def create_analysis(
    tender_id: UUID,
    payload: TenderAnalysisCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderAnalysisRead:
    try:
        analysis = await create_or_update_analysis(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            payload=payload,
        )
    except ScopedNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")
    except AnalysisConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return TenderAnalysisRead.model_validate(analysis)


@router.get("", response_model=TenderAnalysisRead)
async def get_analysis(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderAnalysisRead:
    try:
        await ensure_tender_scoped(db, current_user.company_id, tender_id)
    except ScopedNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tender not found")

    analysis = await get_analysis_scoped(db, current_user.company_id, tender_id)
    if analysis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found")
    return TenderAnalysisRead.model_validate(analysis)


@router.patch("", response_model=TenderAnalysisRead)
async def patch_analysis_endpoint(
    tender_id: UUID,
    payload: TenderAnalysisPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderAnalysisRead:
    try:
        analysis = await patch_analysis(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
            payload=payload,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except AnalysisConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return TenderAnalysisRead.model_validate(analysis)


@router.post("/approve", response_model=TenderAnalysisRead)
async def approve_analysis_endpoint(
    tender_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> TenderAnalysisRead:
    try:
        analysis = await approve_analysis(
            db,
            company_id=current_user.company_id,
            tender_id=tender_id,
            user_id=current_user.id,
        )
    except ScopedNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return TenderAnalysisRead.model_validate(analysis)
