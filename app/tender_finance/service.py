from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tender_finance.model import TenderFinance
from app.tender_finance.schemas import TenderFinanceUpsert
from app.tender_finance.snapshot import build_finance_snapshot
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


async def save_finance_snapshot(
    db: AsyncSession,
    *,
    tender_id: UUID,
    company_id: UUID,
    finance_result: dict,
    contract_value: object = None,
    cost_estimate: object = None,
    participation_cost: object = None,
    win_probability: object = None,
) -> TenderFinance:
    """Persist a compute_finance_v2() result as an immutable snapshot.

    Upserts the tender_finance row, writing profitability_status,
    is_loss_leader, gross_margin, gross_margin_pct, expected_value,
    finance_calculated_at, and the four snapshot input fields.
    All monetary values are stored as NUMERIC (via Decimal) — never float.

    Args:
        tender_id:          Tender UUID.
        company_id:         Company scope.
        finance_result:     Dict returned by compute_finance_v2().
        contract_value:     Contract price (NMCK) fed into the calculation.
        cost_estimate:      Cost-estimate input.
        participation_cost: Participation-cost input.
        win_probability:    Win-probability percentage input.
    """
    snapshot = build_finance_snapshot(
        finance_result,
        contract_value=contract_value,
        cost_estimate=cost_estimate,
        participation_cost=participation_cost,
        win_probability=win_probability,
    )

    finance = await get_finance_scoped(db, company_id, tender_id)
    if finance is None:
        finance = TenderFinance(
            company_id=company_id,
            tender_id=tender_id,
        )
        db.add(finance)

    finance.profitability_status          = snapshot["profitability_status"]
    finance.is_loss_leader                = snapshot["is_loss_leader"]
    finance.gross_margin                  = snapshot["gross_margin"]
    finance.gross_margin_pct              = snapshot["gross_margin_pct"]
    finance.expected_value                = snapshot["expected_value"]
    finance.finance_calculated_at         = snapshot["finance_calculated_at"]
    finance.snapshot_contract_value       = snapshot["snapshot_contract_value"]
    finance.snapshot_cost_estimate        = snapshot["snapshot_cost_estimate"]
    finance.snapshot_participation_cost   = snapshot["snapshot_participation_cost"]
    finance.snapshot_win_probability      = snapshot["snapshot_win_probability"]

    await db.commit()
    await db.refresh(finance)
    return finance
