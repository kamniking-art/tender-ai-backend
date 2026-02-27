from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tender_analysis.model import TenderAnalysis
from app.tender_analysis.schemas import TenderAnalysisCreate, TenderAnalysisPatch, TenderAnalysisStatus
from app.tenders.service import get_tender_by_id_scoped


class ScopedNotFoundError(Exception):
    pass


class AnalysisConflictError(Exception):
    pass


async def ensure_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID):
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")
    return tender


async def get_analysis_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> TenderAnalysis | None:
    stmt = select(TenderAnalysis).where(
        TenderAnalysis.company_id == company_id,
        TenderAnalysis.tender_id == tender_id,
    )
    return await db.scalar(stmt)


async def create_or_update_analysis(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
    payload: TenderAnalysisCreate,
) -> TenderAnalysis:
    await ensure_tender_scoped(db, company_id, tender_id)

    analysis = await get_analysis_scoped(db, company_id, tender_id)
    reqs = payload.requirements if payload.requirements is not None else {}
    missing = payload.missing_docs if payload.missing_docs is not None else []
    risks = payload.risk_flags if payload.risk_flags is not None else []

    if analysis is None:
        analysis = TenderAnalysis(
            company_id=company_id,
            tender_id=tender_id,
            status=TenderAnalysisStatus.DRAFT.value,
            requirements=reqs,
            missing_docs=missing,
            risk_flags=risks,
            summary=payload.summary,
            created_by=user_id,
            updated_by=user_id,
        )
        db.add(analysis)
    else:
        if analysis.status == TenderAnalysisStatus.APPROVED.value and not payload.overwrite:
            raise AnalysisConflictError("Approved analysis cannot be overwritten")

        analysis.requirements = reqs
        analysis.missing_docs = missing
        analysis.risk_flags = risks
        analysis.summary = payload.summary
        analysis.status = TenderAnalysisStatus.DRAFT.value
        analysis.updated_by = user_id

    await db.commit()
    await db.refresh(analysis)
    return analysis


async def patch_analysis(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
    payload: TenderAnalysisPatch,
) -> TenderAnalysis:
    await ensure_tender_scoped(db, company_id, tender_id)

    analysis = await get_analysis_scoped(db, company_id, tender_id)
    if analysis is None:
        raise ScopedNotFoundError("Analysis not found")

    if analysis.status == TenderAnalysisStatus.APPROVED.value:
        raise AnalysisConflictError("Approved analysis cannot be edited")

    updates = payload.model_dump(exclude_unset=True)
    if "status" in updates and updates["status"] is not None:
        updates["status"] = updates["status"].value

    for field, value in updates.items():
        setattr(analysis, field, value)

    analysis.updated_by = user_id

    await db.commit()
    await db.refresh(analysis)
    return analysis


async def approve_analysis(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
) -> TenderAnalysis:
    await ensure_tender_scoped(db, company_id, tender_id)

    analysis = await get_analysis_scoped(db, company_id, tender_id)
    if analysis is None:
        raise ScopedNotFoundError("Analysis not found")

    if analysis.status != TenderAnalysisStatus.APPROVED.value:
        analysis.status = TenderAnalysisStatus.APPROVED.value
        analysis.updated_by = user_id
        await db.commit()
        await db.refresh(analysis)

    return analysis
