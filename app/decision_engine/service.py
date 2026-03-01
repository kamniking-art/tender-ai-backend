from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.schemas import ExtractedTenderV1
from app.tender_analysis.model import TenderAnalysis
from app.tender_decisions.model import TenderDecision
from app.tenders.model import Tender
from app.tenders.service import get_tender_by_id_scoped


class DecisionEngineError(Exception):
    pass


class ManualRecommendationConflictError(DecisionEngineError):
    pass


class DecisionEngineBadRequestError(DecisionEngineError):
    pass


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _extract_auto_risk_score(analysis: TenderAnalysis | None) -> int | None:
    if analysis is None:
        return None
    payload = (analysis.requirements or {}).get("risk_v1")
    if not isinstance(payload, dict):
        return None
    score = payload.get("score_auto")
    if isinstance(score, int):
        return score
    if isinstance(score, float):
        return int(score)
    return None


def _extract_extracted(analysis: TenderAnalysis | None) -> ExtractedTenderV1 | None:
    if analysis is None:
        return None
    payload = (analysis.requirements or {}).get("extracted_v1")
    if payload is None:
        return None
    try:
        return ExtractedTenderV1.model_validate(payload)
    except Exception:
        return None


def _is_manual_recommendation(decision: TenderDecision) -> bool:
    # If engine meta is absent and recommendation is non-default, treat as manual input.
    has_engine = isinstance(decision.engine_meta, dict) and bool(decision.engine_meta)
    return (decision.recommendation != "unsure") and (not has_engine)


def _resolve_high_security(extracted: ExtractedTenderV1 | None, decision: TenderDecision, tender: Tender) -> bool:
    nmck = extracted.nmck if extracted and extracted.nmck is not None else (decision.nmck or tender.nmck)

    if extracted is not None:
        if extracted.bid_security_pct is not None and extracted.bid_security_pct >= Decimal("5"):
            return True
        if extracted.contract_security_pct is not None and extracted.contract_security_pct >= Decimal("10"):
            return True

    if nmck and nmck > 0:
        if decision.bid_security_amount is not None and (decision.bid_security_amount / nmck) >= Decimal("0.05"):
            return True
        if decision.contract_security_amount is not None and (decision.contract_security_amount / nmck) >= Decimal("0.10"):
            return True

    return False


def _resolve_short_deadline(analysis: TenderAnalysis | None, extracted: ExtractedTenderV1 | None) -> bool:
    codes = {item.get("code") for item in (analysis.risk_flags or []) if isinstance(item, dict)} if analysis else set()
    if "short_deadline" in codes:
        return True

    if extracted and extracted.submission_deadline_at is not None:
        return extracted.submission_deadline_at <= (datetime.now(UTC) + timedelta(days=3))

    return False


def _resolve_harsh_penalties(analysis: TenderAnalysis | None) -> bool:
    codes = {item.get("code") for item in (analysis.risk_flags or []) if isinstance(item, dict)} if analysis else set()
    return "harsh_penalties" in codes


def _margin_score(margin_pct: Decimal | None) -> int:
    if margin_pct is None:
        return 0
    if margin_pct >= Decimal("20"):
        return 40
    if margin_pct >= Decimal("10"):
        return 25
    if margin_pct >= Decimal("0"):
        return 5
    return -40


def _risk_modifier(risk_score: int | None) -> int:
    if risk_score is None:
        return -5
    if risk_score >= 80:
        return -40
    if risk_score >= 60:
        return -25
    if risk_score >= 40:
        return -10
    return 0


def _penalties_modifier(short_deadline: bool, harsh_penalties: bool, high_security: bool) -> int:
    score = 0
    if short_deadline:
        score -= 10
    if harsh_penalties:
        score -= 10
    if high_security:
        score -= 10
    return score


def _recommendation_for_score(score: int) -> Literal["go", "no_go", "unsure"]:
    if score >= 20:
        return "go"
    if score <= -10:
        return "no_go"
    return "unsure"


def compute_decision_engine_v1(
    *,
    margin_pct: Decimal | None,
    margin_value: Decimal | None,
    risk_score: int | None,
    short_deadline: bool,
    harsh_penalties: bool,
    high_security: bool,
) -> dict:
    margin_component = _margin_score(margin_pct)
    risk_component = _risk_modifier(risk_score)
    penalties_component = _penalties_modifier(short_deadline, harsh_penalties, high_security)

    total_score = _clamp(margin_component + risk_component + penalties_component, -100, 100)
    recommendation = _recommendation_for_score(total_score)

    explain: list[str] = [
        f"margin_score={margin_component} (margin_pct={margin_pct})",
        f"risk_modifier={risk_component} (risk_score={risk_score})",
        f"penalties_modifier={penalties_component} (short_deadline={short_deadline}, harsh_penalties={harsh_penalties}, high_security={high_security})",
        f"final_score={total_score} -> recommendation={recommendation}",
    ]

    return {
        "score": total_score,
        "margin_score": margin_component,
        "risk_modifier": risk_component,
        "penalties_modifier": penalties_component,
        "inputs": {
            "margin_pct": float(margin_pct) if margin_pct is not None else None,
            "margin_value": float(margin_value) if margin_value is not None else None,
            "risk_score": risk_score,
            "short_deadline": short_deadline,
            "harsh_penalties": harsh_penalties,
            "high_security": high_security,
        },
        "explain": explain,
        "computed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "recommendation": recommendation,
    }


async def _get_analysis_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> TenderAnalysis | None:
    return await db.scalar(
        select(TenderAnalysis).where(
            TenderAnalysis.company_id == company_id,
            TenderAnalysis.tender_id == tender_id,
        )
    )


async def _get_decision_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> TenderDecision | None:
    return await db.scalar(
        select(TenderDecision).where(
            TenderDecision.company_id == company_id,
            TenderDecision.tender_id == tender_id,
        )
    )


async def _get_or_create_decision(db: AsyncSession, company_id: UUID, tender_id: UUID, user_id: UUID) -> TenderDecision:
    decision = await _get_decision_scoped(db, company_id, tender_id)
    if decision is not None:
        return decision

    decision = TenderDecision(
        company_id=company_id,
        tender_id=tender_id,
        recommendation="unsure",
        rationale=[],
        assumptions=[],
        risk_score=0,
        risk_flags=[],
        need_bid_security=False,
        need_contract_security=False,
        created_by=user_id,
        updated_by=user_id,
        engine_meta={},
    )
    db.add(decision)
    await db.flush()
    return decision


async def recompute_decision_engine_v1(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    user_id: UUID,
    force: bool,
) -> tuple[TenderDecision, dict]:
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise DecisionEngineBadRequestError("Tender not found")

    decision = await _get_or_create_decision(db, company_id, tender_id, user_id)

    if _is_manual_recommendation(decision) and not force:
        raise ManualRecommendationConflictError("manual recommendation set")

    analysis = await _get_analysis_scoped(db, company_id, tender_id)
    extracted = _extract_extracted(analysis)

    risk_score = decision.risk_score if decision.risk_score is not None else _extract_auto_risk_score(analysis)
    short_deadline = _resolve_short_deadline(analysis, extracted)
    harsh_penalties = _resolve_harsh_penalties(analysis)
    high_security = _resolve_high_security(extracted, decision, tender)

    engine = compute_decision_engine_v1(
        margin_pct=decision.expected_margin_pct,
        margin_value=decision.expected_margin_value,
        risk_score=risk_score,
        short_deadline=short_deadline,
        harsh_penalties=harsh_penalties,
        high_security=high_security,
    )

    decision.recommendation = engine["recommendation"]
    decision.engine_meta = engine
    decision.updated_by = user_id

    await db.commit()
    await db.refresh(decision)

    return decision, engine


async def get_decision_engine_scoped(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
) -> tuple[TenderDecision, dict | None]:
    tender = await get_tender_by_id_scoped(db, company_id, tender_id)
    if tender is None:
        raise DecisionEngineBadRequestError("Tender not found")

    decision = await _get_decision_scoped(db, company_id, tender_id)
    if decision is None:
        raise DecisionEngineBadRequestError("Decision not found")

    return decision, decision.engine_meta if isinstance(decision.engine_meta, dict) and decision.engine_meta else None
