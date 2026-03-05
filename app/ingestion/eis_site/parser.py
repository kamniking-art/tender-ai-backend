from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from urllib.parse import urljoin

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

# ЕИС обычно использует 19-значные номера извещений
_EXTERNAL_ID_RE = re.compile(r"\b\d{19}\b")
_ENTRY_BLOCK_RE = re.compile(r'<div class="search-registry-entry-block\b', re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_TITLE_VALUE_RE = re.compile(
    r'<div class="registry-entry__body-title">\s*Объект закупки\s*</div>\s*<div class="registry-entry__body-value">(.*?)</div>',
    re.IGNORECASE | re.DOTALL,
)
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?")
_MONEY_RE = re.compile(r"(\d[\d\s.,]{2,})")
_TRAILING_TAG_GARBAGE_RE = re.compile(r"<[^>]*$")
_FIELD_BY_TITLE_RE = re.compile(
    r'<div class="registry-entry__body-title">\s*(?P<label>[^<]+?)\s*</div>\s*'
    r'(?P<body><div class="registry-entry__body-(?:value|href)">.*?</div>)',
    re.IGNORECASE | re.DOTALL,
)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data:
            self.parts.append(data)

    def text(self) -> str:
        return " ".join(self.parts)


@dataclass
class EISSiteCandidate:
    external_id: str
    title: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    submission_deadline: datetime | None = None
    nmck: Decimal | None = None
    customer_name: str | None = None


@dataclass
class ParseResult:
    candidates: list[EISSiteCandidate]
    errors: list[str]


def _clean_text(value: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(value)
    extracted = extractor.text()
    text = html.unescape(extracted if extracted.strip() else _TAG_RE.sub(" ", value))
    text = _TRAILING_TAG_GARBAGE_RE.sub(" ", text)
    text = text.replace("<", " ").replace(">", " ")
    return _SPACE_RE.sub(" ", text).strip()


def _clean_title(value: str | None) -> str | None:
    if not value:
        return None
    text = _clean_text(value)
    if not text:
        return None
    return text[:500]


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_money(value: str | None) -> Decimal | None:
    if not value:
        return None
    match = _MONEY_RE.search(value)
    if not match:
        return None
    raw = match.group(1).replace(" ", "").replace(",", ".")
    try:
        return Decimal(raw)
    except Exception:
        return None


def parse_search_page(html_text: str, base_url: str) -> ParseResult:
    errors: list[str] = []
    try:
        starts = [m.start() for m in _ENTRY_BLOCK_RE.finditer(html_text)]
        if not starts:
            return ParseResult(candidates=[], errors=errors)

        candidates: list[EISSiteCandidate] = []
        seen: set[str] = set()

        for idx, start in enumerate(starts):
            end = starts[idx + 1] if idx + 1 < len(starts) else len(html_text)
            block = html_text[start:end]
            id_match = _EXTERNAL_ID_RE.search(block)
            if not id_match:
                continue
            external_id = id_match.group(0)
            if external_id in seen:
                continue
            seen.add(external_id)

            title = _extract_title(block)
            url = _extract_url(block, external_id, base_url)
            customer_name = _extract_by_labels(block, ["Заказчик", "Организация"])

            published_at = _parse_datetime(
                _extract_by_labels(block, ["Размещено", "Дата размещения", "Дата размещения извещения"])
            )
            deadline = _parse_datetime(
                _extract_by_labels(
                    block,
                    [
                        "Окончание подачи заявок",
                        "Дата окончания подачи заявок",
                        "Дата и время окончания подачи заявок",
                    ],
                )
            )
            nmck = _parse_money(
                _extract_by_labels(
                    block,
                    [
                        "Начальная цена",
                        "Начальная (максимальная) цена контракта",
                        "НМЦК",
                    ],
                )
            )

            # fallback extraction if labels are missing in current layout
            if not published_at or not deadline:
                dates = _DATE_RE.findall(block)
                if dates and not published_at:
                    published_at = _parse_datetime(" ".join(x for x in dates[0] if x))
                if len(dates) > 1 and not deadline:
                    deadline = _parse_datetime(" ".join(x for x in dates[1] if x))
            if nmck is None:
                nmck = _parse_money(block)

            candidates.append(
                EISSiteCandidate(
                    external_id=external_id,
                    title=title,
                    url=url,
                    published_at=published_at,
                    submission_deadline=deadline,
                    nmck=nmck,
                    customer_name=customer_name,
                )
            )

        return ParseResult(candidates=candidates, errors=errors)
    except Exception as exc:  # best-effort
        errors.append(f"parse_exception:{exc.__class__.__name__}")
        return ParseResult(candidates=[], errors=errors)


def _extract_title(block: str) -> str | None:
    title_match = _TITLE_VALUE_RE.search(block)
    if title_match:
        title = _clean_title(title_match.group(1))
        if title:
            return title
    return _extract_by_labels(block, ["Наименование закупки"])


def _extract_url(block: str, external_id: str, base_url: str) -> str | None:
    hrefs = _HREF_RE.findall(block)
    selected: str | None = None
    for href in hrefs:
        if external_id in href and "common-info" in href:
            selected = href
            break
    if not selected:
        for href in hrefs:
            if external_id in href and "/notice/" in href:
                selected = href
                break
    if not selected:
        return None
    return urljoin(base_url, selected)


def _extract_by_labels(block: str, labels: list[str]) -> str | None:
    normalized = {label.lower() for label in labels}
    for match in _FIELD_BY_TITLE_RE.finditer(block):
        label = _clean_text(match.group("label")).lower()
        if label not in normalized:
            continue
        return _clean_title(match.group("body"))
    return None
