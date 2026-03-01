from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tender_finance.model import TenderFinance
from app.tender_finance.schemas import TenderFinanceUpsert
from app.tenders.service import get_tender_by_id_scoped


class ScopedNotFoundError(Exception):
    pass


async def ensure_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID):
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")
    return tender


async def get_finance_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> TenderFinance | None:
    stmt = select(TenderFinance).where(
        TenderFinance.company_id == company_id,
        TenderFinance.tender_id == tender_id,
    )
    return await db.scalar(stmt)


async def upsert_finance(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    payload: TenderFinanceUpsert,
) -> TenderFinance:
    await ensure_tender_scoped(db, company_id, tender_id)
    finance = await get_finance_scoped(db, company_id, tender_id)

    if finance is None:
        finance = TenderFinance(
            company_id=company_id,
            tender_id=tender_id,
        )
        db.add(finance)

    finance.cost_estimate = payload.cost_estimate
    finance.participation_cost = payload.participation_cost
    finance.win_probability = payload.win_probability
    finance.notes = payload.notes

    await db.commit()
    await db.refresh(finance)
    return finance
