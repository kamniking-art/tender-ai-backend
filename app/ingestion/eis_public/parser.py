import html
import logging
import re
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

from app.ingestion.eis_public.schemas import EISCandidate

logger = logging.getLogger("uvicorn.error")

_EXTERNAL_ID_RE = re.compile(r"\b\d{19}\b")
_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", value))).strip()


def _parse_money(text: str) -> Decimal | None:
    m = re.search(r"(\d[\d\s.,]*)", text)
    if not m:
        return None
    num = m.group(1).replace(" ", "").replace(",", ".")
    try:
        return Decimal(num)
    except Exception:
        return None


def _parse_datetime(text: str) -> datetime | None:
    text = text.strip()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def parse_search_results(html_text: str, base_url: str) -> list[EISCandidate]:
    try:
        candidates: list[EISCandidate] = []
        seen: set[str] = set()

        ids = _EXTERNAL_ID_RE.findall(html_text)
        link_map: list[tuple[str, str]] = []
        for href, text in _LINK_RE.findall(html_text):
            link_map.append((href, _clean_text(text)))

        for external_id in ids:
            if external_id in seen:
                continue
            seen.add(external_id)

            title = None
            url_to_card = None
            for href, text in link_map:
                if external_id in href or external_id in text:
                    title = text or title
                    url_to_card = urljoin(base_url, href)
                    break

            if title is None:
                context_idx = html_text.find(external_id)
                if context_idx != -1:
                    snippet = html_text[max(0, context_idx - 400) : context_idx + 400]
                    title = _clean_text(snippet)

            url_to_viewxml = url_to_card if url_to_card and "viewXml" in url_to_card else None

            candidates.append(
                EISCandidate(
                    external_id=external_id,
                    title=title,
                    url_to_card=url_to_card,
                    url_to_viewxml=url_to_viewxml,
                )
            )

        return candidates
    except Exception:
        logger.warning("ingestion parser warning: failed to parse search page", exc_info=True)
        return []


def parse_viewxml(xml_text: str) -> EISCandidate | None:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        logger.warning("ingestion parser warning: invalid XML", exc_info=True)
        return None

    def find_text(*tags: str) -> str | None:
        for tag in tags:
            node = root.find(f".//{tag}")
            if node is not None and node.text:
                return node.text.strip()
        return None

    ext_id = find_text("purchaseNumber", "regNum", "notificationNumber")
    if not ext_id:
        return None

    title = find_text("purchaseObjectInfo", "purchaseName", "name")
    customer_name = find_text("customer/fullName", "customerInfo/fullName", "customerName")
    region = find_text("region", "deliveryPlace")

    raw_nmck = find_text("maxPrice", "initialSum", "price")
    nmck = _parse_money(raw_nmck or "") if raw_nmck else None

    raw_pub = find_text("publishDate", "createDate")
    raw_deadline = find_text("endDate", "applicationDeadline", "submissionCloseDate")

    published_at = _parse_datetime(raw_pub or "") if raw_pub else None
    submission_deadline = _parse_datetime(raw_deadline or "") if raw_deadline else None

    procurement_type = None
    law_hint = (find_text("fz", "lawType") or "").lower()
    if "44" in law_hint:
        procurement_type = "44fz"
    elif "223" in law_hint:
        procurement_type = "223fz"

    return EISCandidate(
        external_id=ext_id,
        title=title,
        customer_name=customer_name,
        region=region,
        procurement_type=procurement_type,
        nmck=nmck,
        published_at=published_at,
        submission_deadline=submission_deadline,
    )
