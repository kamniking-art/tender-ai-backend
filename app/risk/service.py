from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.ai_extraction.schemas import ExtractedTenderV1
from app.tenders.nmck import get_sane_nmck
from app.tenders.model import Tender


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _clamp(value: int, min_value: int, max_value: int) -> int:
    return max(min_value, min(max_value, value))


def _to_decimal(value: Decimal | None) -> Decimal | None:
    return value if value is not None else None


def _component_deadline(deadline: datetime | None, explain: list[str]) -> int:
    if deadline is None:
        explain.append("Дедлайн не найден: +10 к риску (unknown).")
        return 10

    now = _now_utc()
    if deadline <= now + timedelta(days=1):
        explain.append("Дедлайн <= 1 дня: +30.")
        return 30
    if deadline <= now + timedelta(days=3):
        explain.append("Дедлайн <= 3 дней: +20.")
        return 20
    if deadline <= now + timedelta(days=7):
        explain.append("Дедлайн <= 7 дней: +10.")
        return 10
    return 0


def _component_securities(extracted: ExtractedTenderV1, nmck: Decimal | None, explain: list[str]) -> int:
    score = 0
    if nmck is None:
        score += 5
        explain.append("НМЦК отсутствует: +5 (unknown) в securities.")

    bid_pct = _to_decimal(extracted.bid_security_pct)
    if bid_pct is not None:
        if bid_pct >= Decimal("5"):
            score += 12
            explain.append("Обеспечение заявки >= 5%: +12.")
        elif bid_pct >= Decimal("2"):
            score += 6
            explain.append("Обеспечение заявки >= 2%: +6.")
    elif extracted.bid_security_amount is not None and nmck is not None and nmck > 0:
        bid_ratio = extracted.bid_security_amount / nmck
        if bid_ratio >= Decimal("0.05"):
            score += 12
            explain.append("Обеспечение заявки amount >= 5% НМЦК: +12.")
        elif bid_ratio >= Decimal("0.02"):
            score += 6
            explain.append("Обеспечение заявки amount >= 2% НМЦК: +6.")

    contract_pct = _to_decimal(extracted.contract_security_pct)
    if contract_pct is not None:
        if contract_pct >= Decimal("10"):
            score += 13
            explain.append("Обеспечение контракта >= 10%: +13.")
        elif contract_pct >= Decimal("5"):
            score += 7
            explain.append("Обеспечение контракта >= 5%: +7.")
    elif extracted.contract_security_amount is not None and nmck is not None and nmck > 0:
        contract_ratio = extracted.contract_security_amount / nmck
        if contract_ratio >= Decimal("0.10"):
            score += 13
            explain.append("Обеспечение контракта amount >= 10% НМЦК: +13.")
        elif contract_ratio >= Decimal("0.05"):
            score += 7
            explain.append("Обеспечение контракта amount >= 5% НМЦК: +7.")

    return _clamp(score, 0, 25)


def _component_penalties(extracted: ExtractedTenderV1, explain: list[str]) -> int:
    penalties = extracted.penalties or []
    text = " ".join(penalties).lower()
    score = 0

    if not penalties:
        score += 5
        explain.append("Раздел penalties пустой: +5 (unknown).")

    if "0,1%" in text or "за каждый день" in text:
        score += 15
        explain.append("Найдены жёсткие ежедневные штрафы: +15.")

    if re.search(r"штраф|неустойк|пени", text):
        score += 10
        explain.append("Найдены штрафные санкции: +10.")

    return _clamp(score, 0, 20)


def _component_requirements(extracted: ExtractedTenderV1, explain: list[str]) -> int:
    requirements = extracted.qualification_requirements or []
    count = len(requirements)
    score = 0

    if count >= 12:
        score += 15
        explain.append("qualification_requirements >= 12: +15.")
    elif count >= 8:
        score += 10
        explain.append("qualification_requirements >= 8: +10.")
    elif count >= 4:
        score += 5
        explain.append("qualification_requirements >= 4: +5.")

    req_text = " ".join(requirements).lower()
    if re.search(r"сро|аналогичных контрактов|опыт выполнения|лиценз", req_text):
        score += 5
        explain.append("Найдены маркеры сложных требований: +5.")

    return _clamp(score, 0, 15)


def _component_unknowns(extracted: ExtractedTenderV1, deadline: datetime | None, nmck: Decimal | None, explain: list[str]) -> int:
    score = 0
    if nmck is None:
        score += 3
        explain.append("Unknowns: nmck отсутствует (+3).")
    if deadline is None:
        score += 3
        explain.append("Unknowns: deadline отсутствует (+3).")
    if extracted.bid_security_required is None and extracted.contract_security_required is None:
        score += 2
        explain.append("Unknowns: security_required неизвестны (+2).")
    if not extracted.penalties:
        score += 2
        explain.append("Unknowns: penalties отсутствуют (+2).")
    return _clamp(score, 0, 10)


def compute_risk_flags(extracted: ExtractedTenderV1, tender: Tender) -> list[dict]:
    flags: list[dict] = []
    now = _now_utc()

    nmck = get_sane_nmck(tender.nmck)
    deadline = extracted.submission_deadline_at or tender.submission_deadline

    if deadline is not None and deadline <= now + timedelta(days=3):
        flags.append(
            {
                "code": "short_deadline",
                "title": "Короткий срок подачи",
                "severity": "high",
                "note": f"Deadline is {deadline.isoformat()}",
            }
        )

    bid_pct = extracted.bid_security_pct
    if (bid_pct is not None and bid_pct >= Decimal("5")) or (
        extracted.bid_security_amount is not None and nmck is not None and extracted.bid_security_amount >= nmck * Decimal("0.05")
    ):
        flags.append(
            {
                "code": "high_bid_security",
                "title": "Высокое обеспечение заявки",
                "severity": "high" if bid_pct is not None and bid_pct >= Decimal("10") else "medium",
                "note": "Bid security looks high for this tender.",
            }
        )

    contract_pct = extracted.contract_security_pct
    if (contract_pct is not None and contract_pct >= Decimal("5")) or (
        extracted.contract_security_amount is not None
        and nmck is not None
        and extracted.contract_security_amount >= nmck * Decimal("0.05")
    ):
        flags.append(
            {
                "code": "high_contract_security",
                "title": "Высокое обеспечение контракта",
                "severity": "high" if contract_pct is not None and contract_pct >= Decimal("10") else "medium",
                "note": "Contract security looks high for this tender.",
            }
        )

    penalties_text = " ".join(extracted.penalties).lower()
    if re.search(r"неустойк|штраф|пени|0,1%", penalties_text, flags=re.IGNORECASE):
        flags.append(
            {
                "code": "harsh_penalties",
                "title": "Жесткие штрафные условия",
                "severity": "medium",
                "note": "Penalty clauses detected in extracted terms.",
            }
        )

    req_text = " ".join(extracted.qualification_requirements).lower()
    if len(extracted.qualification_requirements) >= 8 or re.search(r"сро|опыт выполнения|аналогичных контрактов", req_text):
        flags.append(
            {
                "code": "excessive_requirements",
                "title": "Повышенные квалификационные требования",
                "severity": "medium",
                "note": "Qualification requirements may be restrictive.",
            }
        )

    return flags


def compute_risk_score_v1(extracted: ExtractedTenderV1, tender: Tender) -> dict:
    explain: list[str] = []
    nmck = get_sane_nmck(tender.nmck)
    deadline = extracted.submission_deadline_at or tender.submission_deadline

    components = {
        "deadline": _component_deadline(deadline, explain),
        "securities": _component_securities(extracted, nmck, explain),
        "penalties": _component_penalties(extracted, explain),
        "requirements": _component_requirements(extracted, explain),
        "unknowns": _component_unknowns(extracted, deadline, nmck, explain),
    }

    score_auto = _clamp(sum(int(v) for v in components.values()), 0, 100)

    return {
        "score_auto": score_auto,
        "score_components": components,
        "explain": explain,
        "computed_at": _now_utc().isoformat().replace("+00:00", "Z"),
    }
