from __future__ import annotations

from collections import Counter
from typing import Any

from app.ai_extraction.schemas import ExtractedTenderV1
from app.tender_analysis.model import TenderAnalysis
from app.tenders.model import Tender

RELEVANCE_THRESHOLDS = {
    "high": 60,
    "medium": 30,
}

CATEGORY_KEYWORDS: dict[str, dict[str, Any]] = {
    "stone_granite_memorial": {
        "title": "камень / гранит / памятники",
        "keywords": [
            "гранит",
            "мрамор",
            "плита",
            "памятник",
            "мемориал",
            "стела",
            "щебень",
            "керамогранит",
            "облицовка",
            "бордюр",
            "камень",
            "надгробие",
            "благоустройство",
            "мемориальный",
            "гранитный",
            "плитка",
        ],
    },
    "landscaping_construction": {
        "title": "благоустройство / строительство",
        "keywords": [
            "благоустройство",
            "строительство",
            "ремонт",
            "тротуар",
            "укладка",
            "поставка материалов",
            "дорожные работы",
        ],
    },
    "building_materials": {
        "title": "общестроительные материалы",
        "keywords": [
            "строительные материалы",
            "материалы",
            "цемент",
            "бетон",
            "кирпич",
            "смесь",
        ],
    },
}

NEGATIVE_KEYWORDS = [
    "реагенты",
    "нуклеиновых кислот",
    "медицина",
    "лекарства",
    "аудит",
    "программное обеспечение",
    "хостинг",
    "бензин",
    "связь",
    "обучение",
    "страхование",
]


def _norm(value: str | None) -> str:
    return (value or "").lower().strip()


def _safe_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _extract_docs_text(extracted: ExtractedTenderV1 | None, analysis: TenderAnalysis | None) -> str:
    chunks: list[str] = []
    if extracted is not None:
        chunks.extend(
            [
                _norm(extracted.subject),
                " ".join(_safe_list(extracted.qualification_requirements)),
                " ".join(_safe_list(extracted.tech_parameters)),
                " ".join(_safe_list(extracted.penalties)),
            ]
        )
    if analysis is not None:
        chunks.append(_norm(analysis.summary))
        req = analysis.requirements or {}
        items = req.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    chunks.append(_norm(str(item.get("title", ""))))
                    chunks.append(_norm(str(item.get("text", ""))))
    return " ".join(chunk for chunk in chunks if chunk)


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
    return "низкая"


def compute_relevance_v1(
    *,
    tender: Tender,
    analysis: TenderAnalysis | None,
    extracted: ExtractedTenderV1 | None,
) -> dict[str, Any]:
    title_text = _norm(tender.title)
    customer_text = _norm(tender.customer_name)
    docs_text = _extract_docs_text(extracted, analysis)

    positive_weights = {"title": 14, "customer": 6, "docs": 18}
    positive_cap = {"title": 35, "customer": 20, "docs": 70}

    category_counter: Counter[str] = Counter()
    matched_keywords: set[str] = set()
    positive_score = 0

    for category_code, payload in CATEGORY_KEYWORDS.items():
        kws = payload["keywords"]
        title_hits = _matches(title_text, kws)
        customer_hits = _matches(customer_text, kws)
        docs_hits = _matches(docs_text, kws)
        all_hits = set(title_hits + customer_hits + docs_hits)
        if not all_hits:
            continue

        matched_keywords.update(all_hits)
        category_counter[category_code] += (
            len(title_hits) * positive_weights["title"]
            + len(customer_hits) * positive_weights["customer"]
            + len(docs_hits) * positive_weights["docs"]
        )

        positive_score += min(len(title_hits) * positive_weights["title"], positive_cap["title"])
        positive_score += min(len(customer_hits) * positive_weights["customer"], positive_cap["customer"])
        positive_score += min(len(docs_hits) * positive_weights["docs"], positive_cap["docs"])

    negative_hits = set(_matches(" ".join([title_text, customer_text, docs_text]), NEGATIVE_KEYWORDS))
    penalty = min(len(negative_hits) * 18, 65)

    score = max(0, min(100, positive_score - penalty))
    if positive_score == 0 and penalty > 0:
        score = 5

    if category_counter:
        detected_code = category_counter.most_common(1)[0][0]
        detected_category = CATEGORY_KEYWORDS[detected_code]["title"]
    else:
        detected_category = "нерелевантно / прочее"

    label = _label(score)
    is_relevant = score >= RELEVANCE_THRESHOLDS["high"]

    if matched_keywords:
        reason = (
            f"Найдены релевантные признаки: {', '.join(sorted(matched_keywords)[:6])}."
            + (" Есть нерелевантные маркеры." if negative_hits else "")
        )
    elif negative_hits:
        reason = f"Найдены нерелевантные признаки: {', '.join(sorted(negative_hits)[:4])}."
    else:
        reason = "Релевантные признаки не обнаружены."

    return {
        "score": score,
        "label": label,
        "reason": reason,
        "category": detected_category,
        "matched_keywords": sorted(matched_keywords)[:10],
        "negative_keywords": sorted(negative_hits)[:10],
        "is_relevant": is_relevant,
    }
