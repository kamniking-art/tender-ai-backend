from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_extraction.schemas import ExtractedTenderV1
from app.relevance.service import compute_relevance_v1
from app.tender_analysis.model import TenderAnalysis
from app.tender_decisions.model import TenderDecision
from app.tender_documents.model import TenderDocument
from app.tender_finance.model import TenderFinance
from app.tenders.model import Tender
from app.tenders.service import get_tender_by_id_scoped

MIN_MARGIN_PCT = Decimal("10")
RISK_GO_MAX = 40


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


def _resolve_effective_risk_score(analysis: TenderAnalysis | None, decision: TenderDecision) -> tuple[int | None, str]:
    """
    Canonical risk source is analysis.requirements.risk_v1.score_auto.
    tender_decisions.risk_score is a projection for quick reads/UI.
    """
    auto_score = _extract_auto_risk_score(analysis)
    if auto_score is not None:
        return auto_score, "analysis.risk_v1.score_auto"
    if decision.risk_score is not None:
        return int(decision.risk_score), "decision.risk_score"
    return None, "none"


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


def _keyword_strength(matched_keywords: list[str]) -> int:
    return 40 if matched_keywords else 0


def _nmck_factor(nmck: Decimal | None) -> int:
    if nmck is None:
        return 0
    if nmck < Decimal("200000"):
        return 0
    if nmck < Decimal("1000000"):
        return 10
    if nmck <= Decimal("5000000"):
        return 20
    return 30


def _risk_penalty(risk_score: int | None) -> int:
    if risk_score is None or risk_score <= 20:
        return 0
    if risk_score <= 40:
        return 10
    if risk_score <= 60:
        return 20
    return 30


def _recommendation_for_score(score: int) -> Literal["strong_go", "go", "review", "weak", "no_go"]:
    if score >= 70:
        return "strong_go"
    if score >= 50:
        return "go"
    if score >= 30:
        return "review"
    return "no_go"


def _high_nmck_points(nmck: Decimal | None) -> int:
    if nmck is None:
        return 0
    return 20 if nmck >= Decimal("3000000") else 0


def _small_nmck_penalty(nmck: Decimal | None) -> int:
    if nmck is None:
        return 0
    return 10 if nmck < Decimal("300000") else 0


def _is_fresh_tender(published_at: datetime | None) -> bool:
    if published_at is None:
        return False
    now = datetime.now(UTC)
    dt = published_at if published_at.tzinfo is not None else published_at.replace(tzinfo=UTC)
    age = now - dt
    return timedelta(0) <= age <= timedelta(days=3)


def _build_recommendation_explanation(
    *,
    matched_keywords: list[str],
    negative_keywords: list[str],
    nmck: Decimal | None,
    has_documents: bool,
    is_fresh: bool,
    risk_score: int | None,
) -> dict[str, list[str]]:
    pros: list[str] = []
    cons: list[str] = []
    red_flags: list[str] = []

    if matched_keywords:
        pros.append(f'Совпадение по ключевым словам: {", ".join(matched_keywords[:3])}')
    else:
        cons.append("Нет совпадений по ключевым словам")
        red_flags.append("Пустые ключевые совпадения")

    if nmck is not None and nmck >= Decimal("3000000"):
        pros.append("Высокая сумма тендера")
    elif nmck is not None and nmck < Decimal("300000"):
        cons.append("Слишком маленькая сумма")
        red_flags.append("Низкая НМЦК")

    if is_fresh:
        pros.append("Свежая публикация (до 3 дней)")

    if has_documents:
        pros.append("Есть документы тендера")
    else:
        cons.append("Нет документов")
        red_flags.append("Нет документов")

    if negative_keywords:
        cons.append(f'Найдены нерелевантные слова: {", ".join(negative_keywords[:3])}')
        red_flags.append("Нерелевантная тематика")

    if risk_score is None:
        red_flags.append("Риск не рассчитан")
    elif risk_score >= 60:
        red_flags.append("Повышенный риск")

    return {
        "pros": pros[:5],
        "cons": cons[:5],
        "red_flags": red_flags[:5],
    }


def _recommendation_reason(
    *,
    recommendation: str,
    score: int,
    pros: list[str],
    cons: list[str],
    red_flags: list[str],
) -> str:
    if recommendation in {"strong_go", "go"}:
        main = pros[0] if pros else "Сильных плюсов мало"
        return f"Сильный профиль ({score}/100): {main}."
    if recommendation == "review":
        main = pros[0] if pros else "Есть частичные сигналы"
        caution = red_flags[0] if red_flags else (cons[0] if cons else "Нужна ручная проверка")
        return f"Пограничный профиль ({score}/100): {main}; ограничение: {caution}."
    main = red_flags[0] if red_flags else (cons[0] if cons else "Недостаточно данных")
    return f"Низкий приоритет ({score}/100): {main}."


def _to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def compute_finance_v2(
    *,
    contract_price: Decimal | None,
    cost_estimate: Decimal | None,
    participation_cost: Decimal | None,
    win_probability_pct: Decimal | None,
) -> dict:
    reasons: list[str] = []
    recommendation: Literal["go", "no_go", "requires_analysis"]

    P = contract_price
    C = cost_estimate
    K = participation_cost if participation_cost is not None else Decimal("0")
    W = (win_probability_pct / Decimal("100")) if win_probability_pct is not None else None

    gross_margin: Decimal | None = None
    gross_margin_pct: Decimal | None = None
    expected_value: Decimal | None = None

    if P is None or P <= 0:
        recommendation = "requires_analysis"
        reasons.append("Нет цены контракта.")
    elif C is None or W is None:
        recommendation = "requires_analysis"
        reasons.append("Не заполнены финансовые параметры.")
    else:
        gross_margin = P - C
        gross_margin_pct = (gross_margin / P) * Decimal("100")
        expected_value = (W * gross_margin) - K

        if gross_margin <= 0:
            recommendation = "no_go"
            reasons.append("Отрицательная/нулевая маржа.")
        elif expected_value < 0:
            recommendation = "no_go"
            reasons.append("Отрицательное матожидание.")
        elif expected_value >= 0 and gross_margin_pct >= MIN_MARGIN_PCT:
            recommendation = "go"
            reasons.append("Положительное матожидание и маржа выше порога.")
        else:
            recommendation = "requires_analysis"
            reasons.append("Требуется дополнительная финансовая оценка.")

    return {
        "P": _to_float(P),
        "C": _to_float(C),
        "K": _to_float(K),
        "W": _to_float(W),
        "gross_margin": _to_float(gross_margin),
        "gross_margin_pct": _to_float(gross_margin_pct),
        "expected_value": _to_float(expected_value),
        "finance_recommendation": recommendation,
        "reasons": reasons,
    }


def _recommendation_weight(recommendation: str | None) -> int:
    mapping = {
        "strong_go": 35,
        "go": 28,
        "review": 18,
        "weak": 8,
        "no_go": 0,
    }
    return mapping.get(str(recommendation or "").lower(), 0)


def _relevance_weight(score: int | None) -> int:
    if score is None:
        return 0
    if score >= 80:
        return 20
    if score >= 60:
        return 15
    if score >= 45:
        return 10
    if score >= 20:
        return 5
    return 0


def _decision_weight(score: int | None) -> int:
    if score is None:
        return 0
    if score >= 80:
        return 20
    if score >= 60:
        return 15
    if score >= 40:
        return 10
    if score >= 20:
        return 5
    return 0


def _deadline_weight(deadline: datetime | None) -> int:
    if deadline is None:
        return 0
    now = datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    delta = deadline - now
    if delta.total_seconds() < 0:
        return 0
    if delta <= timedelta(days=1):
        return 25
    if delta <= timedelta(days=3):
        return 18
    if delta <= timedelta(days=7):
        return 10
    return 0


def _priority_label(score: int) -> str:
    if score >= 80:
        return "urgent"
    if score >= 60:
        return "high"
    if score >= 35:
        return "medium"
    return "low"


def _priority_reason(
    *,
    label: str,
    recommendation: str,
    relevance_score: int | None,
    decision_score: int | None,
    deadline_weight: int,
    nmck_weight: int,
    risk_penalty: int,
) -> str:
    rel = relevance_score if relevance_score is not None else 0
    dscore = decision_score if decision_score is not None else 0
    urgency = "дедлайн близко" if deadline_weight >= 18 else "дедлайн не срочный"
    scale = "крупная НМЦК" if nmck_weight >= 10 else "умеренная НМЦК"
    risk_text = "высокий риск" if risk_penalty >= 20 else ("средний риск" if risk_penalty >= 10 else "низкий риск")
    if label == "urgent":
        return f"Срочный просмотр: recommendation={recommendation}, релевантность {rel}, decision score {dscore}, {urgency}, {scale}."
    if label == "high":
        return f"Высокий приоритет: recommendation={recommendation}, релевантность {rel}, decision score {dscore}, {urgency}."
    if label == "medium":
        return f"Средний приоритет: recommendation={recommendation}, релевантность {rel}, {risk_text}, {urgency}."
    return f"Низкий приоритет: recommendation={recommendation}, релевантность {rel}, {risk_text}."


def compute_priority_v1(
    *,
    recommendation: str,
    decision_score: int | None,
    relevance_score: int | None,
    relevance_category: str | None,
    risk_score: int | None,
    nmck: Decimal | None,
    deadline: datetime | None,
    documents_downloaded_count: int,
    extract_ok: bool,
    decision_done: bool,
) -> dict:
    rec_w = _recommendation_weight(recommendation)
    rel_w = _relevance_weight(relevance_score)
    dec_w = _decision_weight(decision_score)
    dl_w = _deadline_weight(deadline)
    nmck_w = _nmck_factor(nmck)
    docs_w = 0
    if documents_downloaded_count > 0:
        docs_w += 5
    if extract_ok:
        docs_w += 5
    if decision_done:
        docs_w += 5
    risk_p = _risk_penalty(risk_score)
    raw = rec_w + rel_w + dec_w + dl_w + nmck_w + docs_w - risk_p
    score = _clamp(raw, 0, 100)
    # Off-topic tenders should never bubble up in review queue.
    if relevance_category == "нерелевантно / прочее" or (relevance_score is not None and relevance_score < 20):
        score = min(score, 34)
    label = _priority_label(score)
    reason = _priority_reason(
        label=label,
        recommendation=recommendation,
        relevance_score=relevance_score,
        decision_score=decision_score,
        deadline_weight=dl_w,
        nmck_weight=nmck_w,
        risk_penalty=risk_p,
    )
    return {
        "score": score,
        "label": label,
        "reason": reason,
        "components": {
            "recommendation_weight": rec_w,
            "relevance_weight": rel_w,
            "decision_weight": dec_w,
            "deadline_weight": dl_w,
            "nmck_weight": nmck_w,
            "docs_weight": docs_w,
            "risk_penalty": risk_p,
        },
    }


def compute_decision_engine_v1(
    *,
    relevance_score: int | None = None,
    matched_keywords: list[str] | None = None,
    negative_keywords: list[str] | None = None,
    nmck: Decimal | None = None,
    has_documents: bool = False,
    published_at: datetime | None = None,
    risk_score: int | None,
    category: str | None = None,
    # Backward-compat inputs from v1:
    margin_pct: Decimal | None = None,
    margin_value: Decimal | None = None,
    short_deadline: bool = False,
    harsh_penalties: bool = False,
    high_security: bool = False,
) -> dict:
    rel = _clamp(relevance_score if relevance_score is not None else 0, 0, 100)
    matched_keywords = matched_keywords or []
    negative_keywords = negative_keywords or []
    keyword_points = _keyword_strength(matched_keywords)
    high_nmck_points = _high_nmck_points(nmck)
    fresh_points = 15 if _is_fresh_tender(published_at) else 0
    docs_points = 15 if has_documents else 0
    negative_penalty = 20 if negative_keywords else 0
    missing_docs_penalty = 20 if not has_documents else 0
    small_nmck_penalty = _small_nmck_penalty(nmck)
    risk_penalty = _risk_penalty(risk_score)

    total_score = _clamp(
        rel
        + keyword_points
        + high_nmck_points
        + fresh_points
        + docs_points
        - negative_penalty
        - missing_docs_penalty
        - small_nmck_penalty
        - risk_penalty,
        0,
        100,
    )
    recommendation = _recommendation_for_score(total_score)
    explanation = _build_recommendation_explanation(
        matched_keywords=matched_keywords,
        negative_keywords=negative_keywords,
        nmck=nmck,
        has_documents=has_documents,
        is_fresh=bool(fresh_points),
        risk_score=risk_score,
    )

    explain: list[str] = [
        f"relevance_score={rel}",
        f"keyword_match=+{keyword_points}",
        f"high_nmck_bonus=+{high_nmck_points}",
        f"fresh_tender_bonus=+{fresh_points}",
        f"documents_bonus=+{docs_points}",
        f"negative_keywords_penalty=-{negative_penalty}",
        f"missing_documents_penalty=-{missing_docs_penalty}",
        f"small_nmck_penalty=-{small_nmck_penalty}",
        f"risk_penalty=-{risk_penalty} (risk_score={risk_score})",
        f"decision_score={total_score} -> recommendation={recommendation}",
    ]
    reason = _recommendation_reason(recommendation=recommendation, score=total_score, **explanation)

    return {
        "score": total_score,
        "decision_score": total_score,
        "recommendation_reason": reason,
        "relevance_component": rel,
        "keyword_strength": keyword_points,
        "nmck_factor": high_nmck_points,
        "fresh_factor": fresh_points,
        "document_factor": docs_points,
        "negative_keywords_penalty": negative_penalty,
        "missing_documents_penalty": missing_docs_penalty,
        "small_nmck_penalty": small_nmck_penalty,
        "risk_penalty": risk_penalty,
        "inputs": {
            "relevance_score": rel,
            "matched_keywords": matched_keywords,
            "negative_keywords": negative_keywords,
            "nmck": float(nmck) if nmck is not None else None,
            "has_documents": has_documents,
            "published_at": published_at.isoformat() if isinstance(published_at, datetime) else None,
            "margin_pct": float(margin_pct) if margin_pct is not None else None,
            "margin_value": float(margin_value) if margin_value is not None else None,
            "risk_score": risk_score,
            "short_deadline": short_deadline,
            "harsh_penalties": harsh_penalties,
            "high_security": high_security,
        },
        "explain": explain,
        "explanation": explanation,
        "computed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "recommendation": recommendation,
    }


def _final_recommendation_from_finance(
    finance_recommendation: Literal["go", "no_go", "requires_analysis"],
    risk_score: int | None,
    relevance_score: int | None,
    base_recommendation: Literal["strong_go", "go", "review", "weak", "no_go"],
) -> Literal["strong_go", "go", "review", "weak", "no_go"]:
    if finance_recommendation == "no_go":
        return "no_go"
    if relevance_score is not None and relevance_score < 20 and base_recommendation in {"strong_go", "go", "review"}:
        return "weak"
    if finance_recommendation == "requires_analysis":
        if base_recommendation in {"strong_go", "go"}:
            return "review"
        return base_recommendation
    if finance_recommendation == "go" and risk_score is not None and risk_score > RISK_GO_MAX and base_recommendation in {"strong_go", "go"}:
        return "review"
    return base_recommendation


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


async def _get_finance_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> TenderFinance | None:
    return await db.scalar(
        select(TenderFinance).where(
            TenderFinance.company_id == company_id,
            TenderFinance.tender_id == tender_id,
        )
    )


async def _documents_count(db: AsyncSession, company_id: UUID, tender_id: UUID) -> int:
    count_stmt = select(func.count()).select_from(TenderDocument).where(
        TenderDocument.company_id == company_id,
        TenderDocument.tender_id == tender_id,
    )
    count = (await db.execute(count_stmt)).scalar_one()
    return int(count or 0)


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
    relevance = compute_relevance_v1(tender=tender, analysis=analysis, extracted=extracted)

    risk_score, risk_source = _resolve_effective_risk_score(analysis, decision)
    short_deadline = _resolve_short_deadline(analysis, extracted)
    harsh_penalties = _resolve_harsh_penalties(analysis)
    high_security = _resolve_high_security(extracted, decision, tender)
    finance = await _get_finance_scoped(db, company_id, tender_id)
    docs_count = await _documents_count(db, company_id, tender_id)
    valid_docs_count_stmt = select(func.count()).select_from(TenderDocument).where(
        TenderDocument.company_id == company_id,
        TenderDocument.tender_id == tender_id,
        func.coalesce(func.lower(TenderDocument.doc_type), "") != "source_import",
    )
    valid_docs_count = int((await db.execute(valid_docs_count_stmt)).scalar_one() or 0)
    has_documents = valid_docs_count > 0
    matched_keywords = [str(item) for item in relevance.get("matched_keywords", []) if isinstance(item, str)]
    negative_keywords = [str(item) for item in relevance.get("negative_keywords", []) if isinstance(item, str)]

    engine = compute_decision_engine_v1(
        relevance_score=relevance.get("score"),
        matched_keywords=matched_keywords,
        negative_keywords=negative_keywords,
        nmck=tender.nmck,
        has_documents=has_documents,
        published_at=tender.published_at,
        margin_pct=decision.expected_margin_pct,
        margin_value=decision.expected_margin_value,
        risk_score=risk_score,
        category=relevance.get("category") if isinstance(relevance.get("category"), str) else None,
        short_deadline=short_deadline,
        harsh_penalties=harsh_penalties,
        high_security=high_security,
    )
    finance_result = compute_finance_v2(
        contract_price=tender.nmck,
        cost_estimate=finance.cost_estimate if finance else None,
        participation_cost=finance.participation_cost if finance else None,
        win_probability_pct=finance.win_probability if finance else None,
    )
    final_recommendation = engine["recommendation"]

    engine["finance"] = finance_result
    engine["relevance"] = relevance
    engine["recommendation_base"] = engine["recommendation"]
    engine["recommendation"] = final_recommendation
    engine["risk_go_max"] = RISK_GO_MAX
    engine["min_margin_pct"] = float(MIN_MARGIN_PCT)
    engine["risk_source"] = risk_source
    if relevance.get("score") is not None and relevance["score"] < 20 and engine["recommendation_base"] in {"strong_go", "go", "review"}:
        engine["explain"].append("relevance_guard=weak (relevance_score<20)")

    priority = compute_priority_v1(
        recommendation=engine["recommendation"],
        decision_score=engine.get("decision_score"),
        relevance_score=relevance.get("score") if isinstance(relevance.get("score"), int) else None,
        relevance_category=relevance.get("category") if isinstance(relevance.get("category"), str) else None,
        risk_score=risk_score,
        nmck=tender.nmck,
        deadline=tender.submission_deadline,
        documents_downloaded_count=docs_count,
        extract_ok=extracted is not None,
        decision_done=True,
    )
    engine["priority"] = priority

    decision.recommendation = engine["recommendation"]
    decision.score = engine.get("score")
    decision.risk_score = int(risk_score) if risk_score is not None else 0
    decision.risk_flags = list(analysis.risk_flags or []) if analysis and isinstance(analysis.risk_flags, list) else []
    decision.decision_score = engine.get("decision_score")
    decision.recommendation_reason = engine.get("recommendation_reason")
    decision.priority_score = priority["score"]
    decision.priority_label = priority["label"]
    decision.priority_reason = priority["reason"]
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
