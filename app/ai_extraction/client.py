from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import httpx

from app.ai_extraction.schemas import ExtractedTenderV1, RemoteExtractorPayload, RemoteExtractorResult
from app.core.config import settings


class AIServiceUnavailableError(RuntimeError):
    pass


class AIServiceBadResponseError(RuntimeError):
    pass


def _parse_decimal(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    cleaned = raw.replace("\u00a0", " ").replace(" ", "")
    cleaned = cleaned.replace(",", ".")
    cleaned = re.sub(r"[^0-9.]", "", cleaned)
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _extract_deadline(text: str) -> datetime | None:
    iso_match = re.search(r"(20\d{2}-\d{2}-\d{2})(?:[T\s](\d{2}:\d{2}(?::\d{2})?))?", text)
    if iso_match:
        date_part = iso_match.group(1)
        time_part = iso_match.group(2) or "00:00:00"
        if len(time_part) == 5:
            time_part += ":00"
        return datetime.fromisoformat(f"{date_part}T{time_part}+00:00")

    ru_match = re.search(r"(\d{2})\.(\d{2})\.(20\d{2})(?:\s+(\d{2}:\d{2}))?", text)
    if ru_match:
        dd, mm, yyyy, hm = ru_match.groups()
        hm = hm or "00:00"
        return datetime.fromisoformat(f"{yyyy}-{mm}-{dd}T{hm}:00+00:00")

    return None


def _extract_first_lines(text: str, limit: int = 3) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[:limit]


def _mock_extract(text: str) -> ExtractedTenderV1:
    nmck_match = re.search(r"(?:НМЦК|NMCK|начальн\w+\s+цена)[^\d]{0,20}([\d\s.,]+)", text, re.IGNORECASE)
    nmck = _parse_decimal(nmck_match.group(1) if nmck_match else None)

    bid_pct_match = re.search(r"обеспечени\w+\s+заявк\w+[^\d]{0,20}(\d{1,2}(?:[.,]\d+)?)\s*%", text, re.IGNORECASE)
    contract_pct_match = re.search(r"обеспечени\w+\s+контракт\w+[^\d]{0,20}(\d{1,2}(?:[.,]\d+)?)\s*%", text, re.IGNORECASE)
    bid_amount_match = re.search(r"обеспечени\w+\s+заявк\w+[^\d]{0,30}([\d\s.,]+)\s*руб", text, re.IGNORECASE)
    contract_amount_match = re.search(r"обеспечени\w+\s+контракт\w+[^\d]{0,30}([\d\s.,]+)\s*руб", text, re.IGNORECASE)

    penalties: list[str] = []
    for keyword in ("неустой", "штраф", "пени", "0,1%"):
        if keyword.lower() in text.lower():
            penalties.append(f"Detected keyword: {keyword}")

    qualification_requirements = [line for line in _extract_first_lines(text, limit=8) if any(k in line.lower() for k in ("опыт", "сро", "квали"))]

    return ExtractedTenderV1(
        schema_version="v1",
        subject=_extract_first_lines(text, limit=1)[0] if _extract_first_lines(text, limit=1) else None,
        nmck=nmck,
        currency="RUB" if nmck is not None else None,
        submission_deadline_at=_extract_deadline(text),
        bid_security_required=(bid_pct_match is not None or bid_amount_match is not None),
        bid_security_amount=_parse_decimal(bid_amount_match.group(1) if bid_amount_match else None),
        bid_security_pct=_parse_decimal(bid_pct_match.group(1) if bid_pct_match else None),
        contract_security_required=(contract_pct_match is not None or contract_amount_match is not None),
        contract_security_amount=_parse_decimal(contract_amount_match.group(1) if contract_amount_match else None),
        contract_security_pct=_parse_decimal(contract_pct_match.group(1) if contract_pct_match else None),
        qualification_requirements=qualification_requirements,
        tech_parameters=_extract_first_lines(text, limit=3),
        penalties=penalties,
        confidence={"overall": 0.61, "nmck": 0.72, "submission_deadline_at": 0.58},
        evidence={"nmck": nmck_match.group(0) if nmck_match else None},
    )


class AIExtractorClient:
    async def extract(self, tender_id: UUID, text: str) -> ExtractedTenderV1:
        mode = (settings.ai_extractor_mode or "mock").strip().lower()
        if mode == "mock":
            return _mock_extract(text)
        if mode != "remote":
            raise AIServiceBadResponseError(f"Unsupported AI_EXTRACTOR_MODE: {settings.ai_extractor_mode}")

        if not settings.ai_extractor_base_url:
            raise AIServiceUnavailableError("AI extractor base URL is not configured")

        payload = RemoteExtractorPayload(tender_id=tender_id, text=text)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if settings.ai_extractor_api_key:
            headers["Authorization"] = f"Bearer {settings.ai_extractor_api_key}"

        url = settings.ai_extractor_base_url.rstrip("/") + "/extract"
        delays = [0.0, 0.5, 1.5]
        last_error: Exception | None = None

        for attempt, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                async with httpx.AsyncClient(timeout=settings.ai_extractor_timeout_sec) as client:
                    response = await client.post(url, headers=headers, json=payload.model_dump(mode="json"))
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt < len(delays) - 1:
                    continue
                raise AIServiceUnavailableError("AI extractor service timeout/unreachable") from exc

            if response.status_code >= 500:
                last_error = RuntimeError(f"upstream status {response.status_code}")
                if attempt < len(delays) - 1:
                    continue
                raise AIServiceUnavailableError("AI extractor service unavailable")

            if response.status_code >= 400:
                raise AIServiceBadResponseError(f"AI extractor returned status {response.status_code}")

            try:
                raw_json = response.json()
            except ValueError as exc:
                raise AIServiceBadResponseError("AI extractor returned invalid JSON") from exc

            try:
                if isinstance(raw_json, dict) and "extracted" in raw_json:
                    return RemoteExtractorResult.model_validate(raw_json).extracted
                return ExtractedTenderV1.model_validate(raw_json)
            except Exception as exc:
                raise AIServiceBadResponseError("AI extractor JSON does not match schema") from exc

        raise AIServiceUnavailableError("AI extractor service unavailable") from last_error
