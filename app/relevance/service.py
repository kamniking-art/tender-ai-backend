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
    "customer": 0,
}

KEYWORD_STRENGTH_WEIGHTS = {
    "strong": 9,
    "medium": 4,
    "weak": 0,
}

CATEGORY_NICHE_MULTIPLIER = {
    "stone_granite_memorial": 1.35,
    "landscaping_construction": 0.55,
    "building_materials": 1.1,
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
            "камень",
            "плита",
            "каменные изделия",
            "изделия из камня",
        ],
        "weak": [],
    },
    "landscaping_construction": {
        "title": "благоустройство / строительство",
        "strong": [
            "благоустройство",
            "благоустрой",
            "строительство",
            "реконструкц",
            "малые архитектурные формы",
            "дорожные работы",
        ],
        "medium": [
            "тротуар",
            "покрытие",
            "озеленение",
            "территория",
            "ремонт",
            "укладка",
            "монтаж",
            "парк",
            "общественная территория",
            "стадион",
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
            "плитк",
            "плитка гранитная",
            "гранитная плитка",
            "щеб",
            "щебень гранитный",
            "бордюр",
            "бордюрный камень",
            "брусчат",
            "тротуарная плитка",
            "строительные материалы",
            "каменные изделия",
            "изделия из камня",
            "облицовочные материалы",
            "стройматериалы",
            "плиты облицовочные",
            "облицов",
        ],
        "medium": [
            "поставка материалов",
            "строительный камень",
            "отделочные материалы",
            "плита",
            "плиты",
            "покрытие",
            "камень",
            "материал",
        ],
        "weak": [
            "материалы",
            "поставка",
            "работы",
            "ремонт",
            "территория",
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
        "вода",
        "водопровод",
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
) -> tuple[
    int,
    dict[str, int],
    dict[str, set[str]],
    set[str],
    Counter[str],
    dict[str, dict[str, int]],
    dict[str, set[str]],
    set[str],
    int,
    set[str],
]:
    score = 0
    category_scores: Counter[str] = Counter()
    category_hit_counts: dict[str, dict[str, int]] = {
        code: {"strong": 0, "medium": 0, "weak": 0} for code in CATEGORY_RULES
    }
    category_keywords: dict[str, set[str]] = {code: set() for code in CATEGORY_RULES}
    matched_by_strength: dict[str, set[str]] = {"strong": set(), "medium": set(), "weak": set()}
    matched_sources: set[str] = set()
    matched_keywords_all: set[str] = set()
    negative_hits: dict[str, set[str]] = defaultdict(set)
    negative_penalty = 0

    for source_name, text in text_sources.items():
        if not text:
            continue
        source_weight = TEXT_SOURCE_WEIGHTS[source_name]
        if source_weight <= 0:
            continue
        for category_code, category_payload in CATEGORY_RULES.items():
            for strength in ("strong", "medium", "weak"):
                keywords = category_payload.get(strength, [])
                hits = _matches(text, keywords)
                if not hits:
                    continue
                hit_count = len(set(hits))
                base_gain = hit_count * source_weight * KEYWORD_STRENGTH_WEIGHTS[strength]
                category_scores[category_code] += base_gain
                category_hit_counts[category_code][strength] += hit_count
                category_keywords[category_code].update(hits)
                gain = int(base_gain * CATEGORY_NICHE_MULTIPLIER[category_code])
                score += gain
                matched_by_strength[strength].update(hits)
                matched_sources.add(source_name)
                matched_keywords_all.update(hits)

        for group, keywords in NEGATIVE_GROUPS.items():
            hits = _matches(text, keywords)
            if not hits:
                continue
            unique_hits = set(hits)
            negative_hits[group].update(unique_hits)
            group_weight = 9 if source_name in {"title", "summary"} else 6
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
        category_hit_counts,
        category_keywords,
        matched_keywords_all,
        negative_penalty + min(sum(len(v) for v in negative_hits.values()) * 4, 30),
        all_negative_hits,
    )


def _detect_category(
    category_scores: Counter[str],
    category_hit_counts: dict[str, dict[str, int]],
    *,
    has_positive: bool,
    negative_penalty: int,
) -> str:
    if not has_positive:
        return "нерелевантно / прочее"

    stone_hits = category_hit_counts["stone_granite_memorial"]
    materials_hits = category_hit_counts["building_materials"]
    landscaping_hits = category_hit_counts["landscaping_construction"]

    if stone_hits["strong"] > 0:
        return CATEGORY_RULES["stone_granite_memorial"]["title"]

    # Materials require concrete item signals, not only generic construction words.
    has_materials_signal = materials_hits["strong"] > 0 or materials_hits["medium"] >= 2
    if has_materials_signal:
        return CATEGORY_RULES["building_materials"]["title"]

    has_landscaping_signal = (landscaping_hits["strong"] + landscaping_hits["medium"]) > 0
    if has_landscaping_signal:
        return CATEGORY_RULES["landscaping_construction"]["title"]

    # One weak material hint without concrete item is not enough for materials category.
    if materials_hits["medium"] == 1 and materials_hits["strong"] == 0:
        return "нерелевантно / прочее"

    top = category_scores.most_common(1)
    if not top:
        return "нерелевантно / прочее"
    category_code, _ = top[0]
    detected = CATEGORY_RULES[category_code]["title"]
    if negative_penalty >= 20:
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
        category_hit_counts,
        category_keywords,
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
        category_hit_counts,
        has_positive=has_positive,
        negative_penalty=negative_penalty,
    )
    if negative_keywords and hits_count["strong"] == 0 and score < 45:
        detected_category = "нерелевантно / прочее"

    label = _label(score)
    is_relevant = score >= RELEVANCE_THRESHOLDS["medium"] and detected_category != "нерелевантно / прочее"

    source_labels = {
        "title": "в названии",
        "summary": "в summary",
        "docs": "в документах",
        "customer": "в данных заказчика/региона",
    }
    source_part = ", ".join(source_labels[s] for s in ("title", "summary", "docs", "customer") if s in matched_sources)

    stone_kw = sorted(category_keywords["stone_granite_memorial"])[:4]
    materials_kw = sorted(category_keywords["building_materials"])[:5]
    landscaping_kw = sorted(category_keywords["landscaping_construction"])[:4]
    if matched_keywords:
        if detected_category == CATEGORY_RULES["building_materials"]["title"]:
            reason = (
                f"Найдены признаки поставки строительных материалов ({', '.join(materials_kw) or 'без детализации'}) "
                f"{source_part or 'в тексте тендера'}."
            )
        elif detected_category == CATEGORY_RULES["stone_granite_memorial"]["title"]:
            reason = (
                f"Найдены признаки гранитной/мемориальной тематики ({', '.join(stone_kw) or 'без детализации'}) "
                f"{source_part or 'в тексте тендера'}."
            )
        elif detected_category == CATEGORY_RULES["landscaping_construction"]["title"]:
            reason = (
                f"Есть общий строительный контекст ({', '.join(landscaping_kw) or 'благоустройство/ремонт'}) "
                "без достаточного числа конкретных material-сигналов."
            )
        else:
            reason = (
                f"Найдены совпадения ({', '.join(sorted(matched_keywords)[:6])}) {source_part or 'в тексте тендера'}; "
                f"сильных={hits_count['strong']}, средних={hits_count['medium']}, слабых={hits_count['weak']}."
            )
        if negative_keywords:
            reason += f" Есть нерелевантные маркеры ({', '.join(sorted(negative_keywords)[:4])}), применен штраф."
    else:
        reason = "Релевантные признаки не обнаружены."
        if negative_keywords:
            reason = f"Тендер содержит признаки нерелевантных направлений: {', '.join(sorted(negative_keywords)[:4])}."

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
