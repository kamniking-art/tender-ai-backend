from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from app.ai_extraction.schemas import ExtractedTenderV1
from app.tender_analysis.model import TenderAnalysis
from app.tenders.model import Tender

RELEVANCE_THRESHOLDS = {
    "high": 70,
    "medium": 45,
    "weak": 20,
}

TEXT_SOURCE_WEIGHTS = {
    "title": 5,
    "summary": 4,
    "docs": 3,
    "customer": 1,
}

KEYWORD_STRENGTH_WEIGHTS = {
    "strong": 7,
    "medium": 4,
    "weak": 1,
}

CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "stone_granite_memorial": {
        "title": "камень / гранит / памятники",
        "strong": [
            "гранит",
            "гранитный",
            "мрамор",
            "памятник",
            "памятники",
            "мемориал",
            "мемориальный",
            "стела",
            "надгробие",
            "надгробный",
            "плита гранитная",
            "гранитная плита",
            "камень облицовочный",
            "бордюр гранитный",
            "мемориальный комплекс",
        ],
        "medium": [
            "керамогранит",
            "облицовка",
            "камень",
            "плита",
            "плитка",
            "бордюр",
            "брусчатка",
            "щебень",
        ],
        "weak": [
            "поставка",
            "материалы",
            "работы",
        ],
    },
    "landscaping_construction": {
        "title": "благоустройство / строительство",
        "strong": [
            "благоустройство",
            "строительство",
            "малые архитектурные формы",
            "дорожные работы",
        ],
        "medium": [
            "тротуар",
            "покрытие",
            "брусчатка",
            "озеленение",
            "территория",
            "ремонт",
            "тротуар",
            "укладка",
            "монтаж",
        ],
        "weak": [
            "поставка",
            "работы",
            "материалы",
        ],
    },
    "building_materials": {
        "title": "строительные материалы",
        "strong": [
            "керамогранит",
            "строительные материалы",
            "облицовочные материалы",
            "стройматериалы",
        ],
        "medium": [
            "плитка",
            "щебень",
            "бордюр",
            "плита",
            "цемент",
            "бетон",
            "кирпич",
            "смесь",
            "облицовка",
        ],
        "weak": [
            "материалы",
            "поставка",
            "работы",
        ],
    },
}

NEGATIVE_GROUPS: dict[str, list[str]] = {
    "медицина/лаборатория": [
        "медицина",
        "лекарства",
        "реагенты",
        "лаборатория",
        "нуклеиновых кислот",
        "анализ",
    ],
    "аудит/консалтинг": [
        "аудит",
        "бухгалтерия",
        "консалтинг",
    ],
    "ит/связь": [
        "программное обеспечение",
        "хостинг",
        "связь",
        "интернет",
        "лицензии",
    ],
    "топливо": [
        "бензин",
        "газ",
        "топливо",
    ],
    "охрана/страхование": [
        "охрана",
        "страхование",
    ],
    "питание/вода": [
        "питьевая вода",
        "продукты",
        "питание",
    ],
    "обучение": [
        "обучение",
        "курсы",
    ],
}


def _norm(value: str | None) -> str:
    return (value or "").lower().strip()


def _safe_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _extract_summary_text(analysis: TenderAnalysis | None) -> str:
    chunks: list[str] = []
    if analysis is not None:
        chunks.append(_norm(analysis.summary))
        req = analysis.requirements or {}
        items = req.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    chunks.append(_norm(str(item.get("title", ""))))
                    chunks.append(_norm(str(item.get("text", ""))))
    return " ".join(chunk for chunk in chunks if chunk).strip()


def _extract_docs_text(extracted: ExtractedTenderV1 | None) -> str:
    if extracted is None:
        return ""
    chunks = [
        _norm(extracted.subject),
        " ".join(_safe_list(extracted.qualification_requirements)),
        " ".join(_safe_list(extracted.tech_parameters)),
        " ".join(_safe_list(extracted.penalties)),
    ]
    return " ".join(chunk for chunk in chunks if chunk).strip()


def _matches(text: str, keywords: list[str]) -> list[str]:
    if not text:
        return []
    found: list[str] = []
    for kw in keywords:
        if kw in text:
            found.append(kw)
    return found


def _label(score: int) -> str:
    if score >= RELEVANCE_THRESHOLDS["high"]:
        return "высокая"
    if score >= RELEVANCE_THRESHOLDS["medium"]:
        return "средняя"
    if score >= RELEVANCE_THRESHOLDS["weak"]:
        return "слабая"
    return "низкая"


def _score_sources(
    *,
    text_sources: dict[str, str],
) -> tuple[int, dict[str, int], dict[str, set[str]], set[str], Counter[str], set[str], int, set[str]]:
    score = 0
    category_scores: Counter[str] = Counter()
    matched_by_strength: dict[str, set[str]] = {"strong": set(), "medium": set(), "weak": set()}
    matched_sources: set[str] = set()
    matched_keywords_all: set[str] = set()
    negative_hits: dict[str, set[str]] = defaultdict(set)
    negative_penalty = 0

    for source_name, text in text_sources.items():
        if not text:
            continue
        source_weight = TEXT_SOURCE_WEIGHTS[source_name]
        for category_code, category_payload in CATEGORY_RULES.items():
            for strength in ("strong", "medium", "weak"):
                keywords = category_payload.get(strength, [])
                hits = _matches(text, keywords)
                if not hits:
                    continue
                hit_count = len(set(hits))
                gain = hit_count * source_weight * KEYWORD_STRENGTH_WEIGHTS[strength]
                score += gain
                category_scores[category_code] += gain
                matched_by_strength[strength].update(hits)
                matched_sources.add(source_name)
                matched_keywords_all.update(hits)

        for group, keywords in NEGATIVE_GROUPS.items():
            hits = _matches(text, keywords)
            if not hits:
                continue
            unique_hits = set(hits)
            negative_hits[group].update(unique_hits)
            group_weight = 7 if source_name in {"title", "summary"} else 5
            negative_penalty += len(unique_hits) * group_weight

    if matched_sources >= {"title", "docs"}:
        score += 10
    elif matched_sources >= {"title", "summary"}:
        score += 6

    if matched_by_strength["strong"] and matched_by_strength["medium"]:
        score += 8

    all_negative_hits = {item for group_hits in negative_hits.values() for item in group_hits}

    return (
        score,
        {key: len(val) for key, val in matched_by_strength.items()},
        matched_by_strength,
        matched_sources,
        category_scores,
        matched_keywords_all,
        negative_penalty + min(sum(len(v) for v in negative_hits.values()) * 4, 30),
        all_negative_hits,
    )


def _detect_category(category_scores: Counter[str], *, has_positive: bool, strong_hits: int, negative_penalty: int) -> str:
    if not has_positive:
        return "нерелевантно / прочее"
    top = category_scores.most_common(1)
    if not top:
        return "нерелевантно / прочее"
    category_code, _ = top[0]
    detected = CATEGORY_RULES[category_code]["title"]
    if negative_penalty >= 30 and strong_hits == 0:
        return "нерелевантно / прочее"
    return detected


def compute_relevance_v2(
    *,
    tender: Tender,
    analysis: TenderAnalysis | None,
    extracted: ExtractedTenderV1 | None,
) -> dict[str, Any]:
    title_text = _norm(tender.title)
    customer_text = _norm(" ".join(filter(None, [tender.customer_name or "", tender.region or "", tender.place_text or ""])))
    summary_text = _extract_summary_text(analysis)
    docs_text = _extract_docs_text(extracted)

    (
        raw_score,
        hits_count,
        matched_by_strength,
        matched_sources,
        category_scores,
        matched_keywords,
        negative_penalty,
        negative_keywords,
    ) = _score_sources(
        text_sources={
            "title": title_text,
            "summary": summary_text,
            "docs": docs_text,
            "customer": customer_text,
        }
    )

    score = max(0, min(100, raw_score - negative_penalty))
    has_positive = (hits_count["strong"] + hits_count["medium"] + hits_count["weak"]) > 0
    if not has_positive and negative_penalty > 0:
        score = min(score, 10)

    detected_category = _detect_category(
        category_scores,
        has_positive=has_positive,
        strong_hits=hits_count["strong"],
        negative_penalty=negative_penalty,
    )

    label = _label(score)
    is_relevant = score >= RELEVANCE_THRESHOLDS["medium"] and detected_category != "нерелевантно / прочее"

    source_labels = {
        "title": "в названии",
        "summary": "в summary",
        "docs": "в документах",
        "customer": "в данных заказчика/региона",
    }
    source_part = ", ".join(source_labels[s] for s in ("title", "summary", "docs", "customer") if s in matched_sources)

    if matched_keywords:
        reason = (
            f"Найдены совпадения ({', '.join(sorted(matched_keywords)[:6])}) {source_part or 'в тексте тендера'}; "
            f"сильных={hits_count['strong']}, средних={hits_count['medium']}, слабых={hits_count['weak']}."
        )
        if negative_penalty > 0:
            reason += " Есть нерелевантные маркеры, применен штраф."
    else:
        reason = "Релевантные признаки не обнаружены."
        if negative_penalty > 0:
            reason = "Тендер содержит признаки нерелевантных направлений."

    return {
        "score": score,
        "label": label,
        "reason": reason,
        "category": detected_category,
        "matched_keywords": sorted(matched_keywords)[:10],
        "negative_keywords": sorted(negative_keywords)[:10],
        "is_relevant": is_relevant,
    }


def compute_relevance_v1(
    *,
    tender: Tender,
    analysis: TenderAnalysis | None,
    extracted: ExtractedTenderV1 | None,
) -> dict[str, Any]:
    return compute_relevance_v2(tender=tender, analysis=analysis, extracted=extracted)
