from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tender_decisions.model import TenderDecision
from app.tender_decisions.schemas import TenderDecisionCreate, TenderDecisionPatch, TenderDecisionRecommend
from app.tenders.service import get_tender_by_id_scoped


class ScopedNotFoundError(Exception):
    pass


class DecisionConflictError(Exception):
    pass


class DecisionValidationError(Exception):
    pass


def _round2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_margin_fields(
    expected_revenue: Decimal | None,
    cogs: Decimal | None,
    logistics_cost: Decimal | None,
    other_costs: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    if expected_revenue is None:
        return None, None

    total_costs = (cogs or Decimal("0")) + (logistics_cost or Decimal("0")) + (other_costs or Decimal("0"))
    margin_value = _round2(expected_revenue - total_costs)

    if expected_revenue == 0:
        return margin_value, None

    margin_pct = _round2((margin_value / expected_revenue) * Decimal("100"))
    return margin_value, margin_pct


def _validate_security_fields(
    need_bid_security: bool,
    bid_security_amount: Decimal | None,
    need_contract_security: bool,
    contract_security_amount: Decimal | None,
) -> None:
    if need_bid_security and bid_security_amount is None:
        raise DecisionValidationError("bid_security_amount is required when need_bid_security is true")
    if need_contract_security and contract_security_amount is None:
        raise DecisionValidationError("contract_security_amount is required when need_contract_security is true")


async def ensure_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID):
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")
    return tender


async def get_decision_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> TenderDecision | None:
    stmt = select(TenderDecision).where(
        TenderDecision.company_id == company_id,
        TenderDecision.tender_id == tender_id,
    )
    return await db.scalar(stmt)


async def create_decision(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
    payload: TenderDecisionCreate,
) -> TenderDecision:
    await ensure_tender_scoped(db, company_id, tender_id)

    existing = await get_decision_scoped(db, company_id, tender_id)
    if existing is not None:
        raise DecisionConflictError("Decision already exists")

    _validate_security_fields(
        payload.need_bid_security,
        payload.bid_security_amount,
        payload.need_contract_security,
        payload.contract_security_amount,
    )

    margin_value, margin_pct = compute_margin_fields(
        payload.expected_revenue,
        payload.cogs,
        payload.logistics_cost,
        payload.other_costs,
    )

    decision = TenderDecision(
        company_id=company_id,
        tender_id=tender_id,
        recommendation=payload.recommendation,
        rationale=payload.rationale,
        assumptions=payload.assumptions,
        nmck=payload.nmck,
        expected_revenue=payload.expected_revenue,
        cogs=payload.cogs,
        logistics_cost=payload.logistics_cost,
        other_costs=payload.other_costs,
        expected_margin_value=margin_value,
        expected_margin_pct=margin_pct,
        risk_score=payload.risk_score,
        risk_flags=payload.risk_flags,
        engine_meta={},
        need_bid_security=payload.need_bid_security,
        bid_security_amount=payload.bid_security_amount,
        need_contract_security=payload.need_contract_security,
        contract_security_amount=payload.contract_security_amount,
        notes=payload.notes,
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)
    return decision


async def patch_decision(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
    payload: TenderDecisionPatch,
) -> TenderDecision:
    await ensure_tender_scoped(db, company_id, tender_id)

    decision = await get_decision_scoped(db, company_id, tender_id)
    if decision is None:
        raise ScopedNotFoundError("Decision not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(decision, field, value)

    _validate_security_fields(
        decision.need_bid_security,
        decision.bid_security_amount,
        decision.need_contract_security,
        decision.contract_security_amount,
    )

    margin_value, margin_pct = compute_margin_fields(
        decision.expected_revenue,
        decision.cogs,
        decision.logistics_cost,
        decision.other_costs,
    )
    decision.expected_margin_value = margin_value
    decision.expected_margin_pct = margin_pct
    decision.updated_by = user_id

    await db.commit()
    await db.refresh(decision)
    return decision


async def set_recommendation(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
    payload: TenderDecisionRecommend,
) -> TenderDecision:
    await ensure_tender_scoped(db, company_id, tender_id)

    decision = await get_decision_scoped(db, company_id, tender_id)
    if decision is None:
        raise ScopedNotFoundError("Decision not found")

    decision.recommendation = payload.recommendation
    if payload.notes is not None:
        decision.notes = payload.notes
    decision.updated_by = user_id

    await db.commit()
    await db.refresh(decision)
    return decision
