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
_LINK_BLOCK_RE = re.compile(r'(<a[^>]+href="([^"]+)"[^>]*>.*?</a>)', re.IGNORECASE | re.DOTALL)
_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?")
_MONEY_RE = re.compile(r"(\d[\d\s.,]{2,})")
_TRAILING_TAG_GARBAGE_RE = re.compile(r"<[^>]*$")
_ID_LINE_RE = re.compile(rf"([^\n\r<>]{{0,400}}{_EXTERNAL_ID_RE.pattern}[^\n\r<>]{{0,400}})")


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
        ids = _EXTERNAL_ID_RE.findall(html_text)
        links = []
        for full_anchor, href in _LINK_BLOCK_RE.findall(html_text):
            anchor_text = _clean_title(full_anchor)
            links.append((href, anchor_text))
        seen: set[str] = set()
        candidates: list[EISSiteCandidate] = []

        for external_id in ids:
            if external_id in seen:
                continue
            seen.add(external_id)

            title: str | None = None
            url: str | None = None

            for href, text in links:
                if external_id in href or (text and external_id in text):
                    title = _clean_title(text) or title
                    url = urljoin(base_url, href)
                    break

            context_idx = html_text.find(external_id)
            context = html_text[max(0, context_idx - 800) : context_idx + 800] if context_idx >= 0 else ""
            if not title and context:
                context_title = None
                id_line = _ID_LINE_RE.search(context)
                if id_line:
                    context_title = _clean_title(id_line.group(1))
                title = context_title or _clean_title(context[:600])

            date_matches = _DATE_RE.findall(context)
            published_at = None
            deadline = None
            if date_matches:
                published_at = _parse_datetime(" ".join(x for x in date_matches[0] if x))
            if len(date_matches) > 1:
                deadline = _parse_datetime(" ".join(x for x in date_matches[1] if x))

            nmck = _parse_money(context)

            customer_name = None
            for marker in ("Заказчик", "Организация"):
                marker_pos = context.lower().find(marker.lower())
                if marker_pos >= 0:
                    snippet = _clean_title(context[marker_pos : marker_pos + 220]) or ""
                    customer_name = snippet.replace(marker, "").strip(" :.-") or None
                    break

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
