"""Opportunity Report generator — deterministic, pure function, no IO.

Builds an OpportunityReport from already-computed data:
  - FitScoreComponents (what matched / what didn't)
  - risk_score + risk_flags (from TenderDecision)
  - ExtractedTenderV1 (qualification_requirements, licenses, sro_required, deadline)
  - submission deadline delta
  - TenderDecision recommendation + score
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.opportunity_report.schema import OpportunityReport


# ── Component label maps ──────────────────────────────────────────────────────

_STRENGTH_LABELS: dict[str, str] = {
    "okved":           "ОКВЭД компании совпадает с профилем тендера",
    "sro":             "СРО: компания соответствует требованиям",
    "license":         "Лицензии соответствуют требованиям тендера",
    "experience":      "Требования к опыту выполнены",
    "finance":         "Финансовое обеспечение доступно",
    "region_ok":       "Тендер в рабочем регионе компании",
    "nmck_range_ok":   "НМЦК в допустимом диапазоне",
    "capacity_ok":     "Есть свободная мощность для нового проекта",
    "economics_ok":    "Ожидаемая маржа соответствует минимуму",
    "risk_ok":         "Уровень риска в допустимых пределах",
}

_RISK_LABELS: dict[str, str] = {
    "okved":           "ОКВЭД не совпадает — тендер не по профилю компании",
    "sro":             "Требуется СРО — компания не имеет допуска",
    "license":         "Требуется лицензия — у компании нет подходящей",
    "experience":      "Требуется опыт — данных нет или не подтверждён",
    "finance":         "Финансовое обеспечение может быть недостаточным",
    "region_ok":       "Тендер вне рабочего региона — штраф к оценке",
    "nmck_range_ok":   "НМЦК вне допустимого диапазона компании",
    "capacity_ok":     "Превышен лимит одновременных проектов",
    "economics_ok":    "Ожидаемая маржа ниже минимального порога",
    "risk_ok":         "Уровень риска превышает допустимый порог",
}

_MISSING_LABELS: dict[str, str] = {
    "okved":           "ОКВЭД тендера не определён — соответствие неизвестно",
    "sro":             "Данные о СРО в профиле компании не заполнены",
    "license":         "Данные о лицензиях в профиле компании не заполнены",
    "experience":      "Требования к опыту в тендере не указаны",
    "finance":         "Сумма обеспечения не извлечена из документов",
    "region_ok":       "Список рабочих регионов компании не задан",
    "nmck_range_ok":   "Диапазон НМЦК компании не настроен",
    "capacity_ok":     "Текущая загрузка компании не указана",
    "economics_ok":    "Минимальная маржа или оценка тендера не задана",
    "risk_ok":         "Допустимый уровень риска в профиле не настроен",
}

_RISK_FLAG_LABELS: dict[str, str] = {
    "short_deadline":        "Короткий дедлайн — мало времени на подготовку заявки",
    "high_security":         "Высокое обеспечение заявки / контракта",
    "harsh_penalties":       "Жёсткие штрафные санкции в договоре",
    "missing_documents":     "Документы тендера не загружены",
    "no_okved_match":        "ОКВЭД не совпадает",
    "no_sro":                "Отсутствует СРО",
    "low_fit_score":         "Низкое соответствие профилю компании",
    "high_nmck":             "Крупный контракт — требует согласования",
}


def _days_until(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - now
    return int(delta.total_seconds() / 86400)


def generate(
    *,
    components,           # FitScoreComponents
    risk_score: int | None,
    risk_flags: list,
    extracted,            # ExtractedTenderV1 | None
    recommendation: str,
    score: int | None,
    nmck: Decimal | None = None,
) -> OpportunityReport:
    """Build OpportunityReport from pre-computed tender analysis data.

    All inputs are already computed — no DB calls, no LLM.
    """
    strengths: list[str] = []
    risks: list[str] = []
    missing: list[str] = []
    required_docs: list[str] = []
    actions: list[str] = []

    # ── FitScore components ───────────────────────────────────────────────────
    component_dict = components.model_dump() if hasattr(components, "model_dump") else {}

    for key, value in component_dict.items():
        if value is True:
            label = _STRENGTH_LABELS.get(key)
            if label:
                strengths.append(label)
        elif value is False:
            label = _RISK_LABELS.get(key)
            if label:
                risks.append(label)
        elif value is None:
            label = _MISSING_LABELS.get(key)
            if label:
                missing.append(label)

    # ── Risk score ────────────────────────────────────────────────────────────
    if risk_score is not None:
        if risk_score >= 70:
            risks.append(f"Высокий риск-балл: {risk_score}/100")
        elif risk_score >= 40:
            risks.append(f"Умеренный риск-балл: {risk_score}/100")
        else:
            strengths.append(f"Низкий риск-балл: {risk_score}/100")
    else:
        missing.append("Риск-балл не рассчитан")

    # ── Risk flags ────────────────────────────────────────────────────────────
    seen_flags: set[str] = set()
    for flag in (risk_flags or []):
        code = flag.get("code", "") if isinstance(flag, dict) else str(flag)
        if code and code not in seen_flags:
            seen_flags.add(code)
            label = _RISK_FLAG_LABELS.get(code)
            if label:
                risks.append(label)

    # ── Extracted requirements → required_docs ────────────────────────────────
    if extracted is not None:
        if extracted.sro_required is True:
            required_docs.append("Свидетельство СРО")
        if extracted.sro_required is None:
            missing.append("Требование СРО не определено из документов")

        for req in (extracted.qualification_requirements or [])[:5]:
            if req.strip():
                required_docs.append(req.strip())

        for lic in (extracted.licenses or [])[:3]:
            if lic:
                required_docs.append(f"Лицензия: {lic}")

        if extracted.experience_required:
            required_docs.append(f"Опыт: {extracted.experience_required}")

        if extracted.bid_security_required is True:
            amt = extracted.bid_security_amount
            pct = extracted.bid_security_pct
            if amt:
                required_docs.append(f"Обеспечение заявки: {int(amt):,} ₽".replace(",", " "))
            elif pct:
                required_docs.append(f"Обеспечение заявки: {pct}% от НМЦК")
            else:
                required_docs.append("Обеспечение заявки (сумма не извлечена)")

        if extracted.bank_guarantee_required is True:
            required_docs.append("Банковская гарантия")

        # Deadline urgency
        days = _days_until(extracted.submission_deadline_at)
        if days is not None:
            if days <= 1:
                risks.append(f"Дедлайн через {days} дн. — крайне мало времени")
            elif days <= 3:
                risks.append(f"Дедлайн через {days} дн. — мало времени")
            elif days <= 7:
                missing.append(f"Дедлайн через {days} дн. — нужно ускориться")
            else:
                strengths.append(f"Дедлайн через {days} дн. — достаточно времени")
    else:
        missing.append("Документы тендера не проанализированы — требования неизвестны")

    # ── Recommended actions ───────────────────────────────────────────────────
    rec = recommendation.lower()
    if rec in ("go", "strong_go"):
        actions.append("Подготовить заявку и пакет документов")
        if required_docs:
            actions.append("Собрать документы из списка требований")
        if risks:
            actions.append("Проверить риски перед подачей заявки")
        if extracted and extracted.submission_deadline_at:
            actions.append("Отслеживать дедлайн подачи заявки")
    elif rec == "review":
        actions.append("Изучить тендерную документацию подробнее")
        if missing:
            actions.append("Заполнить недостающие данные в профиле компании")
        actions.append("Проконсультироваться с руководителем перед решением")
    elif rec in ("no_go", "weak"):
        if risks:
            actions.append(f"Основная причина отказа: {risks[0]}")
        actions.append("Зафиксировать тендер в базе для будущей статистики")
    else:
        actions.append("Запустить полный анализ тендерной документации")
        actions.append("Заполнить профиль компании для точной оценки")

    # Deduplicate while preserving order
    def _dedup(lst: list[str]) -> list[str]:
        seen: set[str] = set()
        return [x for x in lst if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

    return OpportunityReport(
        strengths=_dedup(strengths),
        risks=_dedup(risks),
        missing_information=_dedup(missing),
        required_documents=_dedup(required_docs),
        recommended_actions=_dedup(actions),
        recommendation=recommendation,
        score=score,
    )
