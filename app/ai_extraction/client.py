from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx

from app.ai_extraction.interfaces import ExtractionProvider, ExtractionProviderError, ExtractionProviderResult
from app.ai_extraction.schemas import ExtractedTenderV1, RemoteExtractorPayload, RemoteExtractorResult
from app.core.config import settings


PARSER_VERSION = "1.0"
CHUNKING_VERSION = "1.0"   # build_semantic_chunks domain routing
ROUTING_VERSION = "1.0"    # _DOMAIN_REGISTRY + _FILENAME_BONUS_RULES
NORMALIZER_VERSION = "1.0" # build_normalized_text / text preprocessing


def pipeline_versions() -> dict[str, str]:
    """Return a snapshot of all pipeline component versions."""
    return {
        "parser": PARSER_VERSION,
        "chunking": CHUNKING_VERSION,
        "routing": ROUTING_VERSION,
        "normalizer": NORMALIZER_VERSION,
    }


# Retry policy per error class.
# TimeoutException  → no retry  (already timed out, immediate retry won't help)
# NetworkError      → retry once after 1 s  (transient connection reset)
# HTTP 429          → exponential backoff, up to 2 retries
# HTTP 5xx          → exponential backoff, up to 2 retries
# HTTP 4xx (non-429)→ fail immediately  (our payload is wrong)
# Invalid JSON      → fail immediately
_RETRY_NETWORK: list[float] = [1.0]
_RETRY_RATE_LIMIT: list[float] = [2.0, 5.0]
_RETRY_SERVER_ERROR: list[float] = [1.0, 3.0]


class AIServiceUnavailableError(RuntimeError):
    pass


class AIServiceBadResponseError(RuntimeError):
    pass


_CLAUDE_FACT_FIELDS = (
    "sro_required",
    "licenses",
    "experience_required",
    "security_amount",
    "deadline_days",
    "bank_guarantee",
)


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


def _chunk_text(text: str, *, max_chars: int) -> list[str]:
    payload = (text or "").strip()
    if not payload:
        return []
    if len(payload) <= max_chars:
        return [payload]

    chunks: list[str] = []
    current = ""
    for line in payload.splitlines():
        piece = line + "\n"
        if len(piece) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            start = 0
            while start < len(piece):
                chunks.append(piece[start : start + max_chars])
                start += max_chars
            continue
        if len(current) + len(piece) <= max_chars:
            current += piece
        else:
            if current:
                chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


def _safe_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _safe_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d]", "", value)
        if cleaned:
            try:
                return int(cleaned)
            except Exception:
                return None
    return None


def _safe_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        try:
            return max(0.0, min(1.0, float(value.strip())))
        except Exception:
            return None
    return None


def _strict_fact_item(raw: object) -> dict[str, object]:
    if not isinstance(raw, dict):
        return {"value": None, "confidence": 0.0, "evidence": ""}
    return {
        "value": raw.get("value"),
        "confidence": _safe_float(raw.get("confidence")) or 0.0,
        "evidence": str(raw.get("evidence") or ""),
    }


def _build_claude_prompt(chunk_text: str) -> str:
    schema = {
        key: {"value": None, "confidence": 0.0, "evidence": ""}
        for key in _CLAUDE_FACT_FIELDS
    }
    return (
        "Ты извлекаешь только факты из тендерного текста. Верни ТОЛЬКО JSON без markdown/комментариев.\n"
        "Если факта нет, ставь value=null (или [] для licenses), confidence<=0.5 и пустой evidence.\n"
        "Строгий формат:\n"
        f"{json.dumps(schema, ensure_ascii=False)}\n\n"
        "Текст:\n"
        f"{chunk_text}"
    )


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


class MockExtractorProvider(ExtractionProvider):
    async def extract(
        self,
        *,
        tender_id: UUID,
        company_id: UUID,
        tender_context: dict,
        text: str,
        chunks: dict[str, str] | None = None,
    ) -> ExtractionProviderResult:
        started = time.perf_counter()
        source_text = text or "\n\n".join((chunks or {}).values())
        extracted = _mock_extract(source_text)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return ExtractionProviderResult(
            extracted=extracted,
            extract_meta={
                "provider": "mock",
                "model": "deterministic-regex-v1",
                "latency_ms": latency_ms,
                "doc_coverage": 1.0,
                "confidence": extracted.confidence.get("overall", 0.0),
                "parser_version": PARSER_VERSION,
                "warnings": [],
                "sources": [str(tender_id)],
            },
        )


class RemoteExtractorProvider(ExtractionProvider):
    async def extract(
        self,
        *,
        tender_id: UUID,
        company_id: UUID,
        tender_context: dict,
        text: str,
        chunks: dict[str, str] | None = None,
    ) -> ExtractionProviderResult:
        if not settings.ai_extractor_base_url:
            raise ExtractionProviderError("PROVIDER_ERROR", "AI extractor base URL is not configured")

        payload = RemoteExtractorPayload(tender_id=tender_id, text=text, chunks=chunks or None)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if settings.ai_extractor_api_key:
            headers["Authorization"] = f"Bearer {settings.ai_extractor_api_key}"

        url = settings.ai_extractor_base_url.rstrip("/") + "/extract"
        started = time.perf_counter()
        attempt = 0
        while True:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=settings.ai_extractor_timeout_sec) as client:
                    response = await client.post(url, headers=headers, json=payload.model_dump(mode="json"))
            except httpx.TimeoutException as exc:
                raise ExtractionProviderError("PROVIDER_TIMEOUT", "AI extractor service timeout") from exc
            except httpx.NetworkError as exc:
                retry_idx = attempt - 1
                if retry_idx < len(_RETRY_NETWORK):
                    delay = _RETRY_NETWORK[retry_idx]
                    logger.warning(
                        "retry: provider=remote error=NetworkError attempt=%d delay=%.1fs",
                        attempt, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ExtractionProviderError("PROVIDER_TIMEOUT", "AI extractor service unreachable") from exc

            if response.status_code == 429:
                retry_idx = attempt - 1
                if retry_idx < len(_RETRY_RATE_LIMIT):
                    delay = _RETRY_RATE_LIMIT[retry_idx]
                    logger.warning(
                        "retry: provider=remote error=RateLimit attempt=%d delay=%.1fs",
                        attempt, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ExtractionProviderError("PROVIDER_ERROR", "AI extractor rate limit exceeded")

            if response.status_code >= 500:
                retry_idx = attempt - 1
                if retry_idx < len(_RETRY_SERVER_ERROR):
                    delay = _RETRY_SERVER_ERROR[retry_idx]
                    logger.warning(
                        "retry: provider=remote error=ServerError(%d) attempt=%d delay=%.1fs",
                        response.status_code, attempt, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ExtractionProviderError("PROVIDER_ERROR", f"upstream status {response.status_code}")

            if response.status_code >= 400:
                raise ExtractionProviderError("PROVIDER_ERROR", f"AI extractor returned status {response.status_code}")

            try:
                raw_json = response.json()
            except ValueError as exc:
                raise ExtractionProviderError("VALIDATION_ERROR", "AI extractor returned invalid JSON") from exc

            try:
                if isinstance(raw_json, dict) and "extracted" in raw_json:
                    parsed = RemoteExtractorResult.model_validate(raw_json)
                    extracted = parsed.extracted
                    raw_meta = raw_json.get("extract_meta") if isinstance(raw_json.get("extract_meta"), dict) else {}
                else:
                    extracted = ExtractedTenderV1.model_validate(raw_json)
                    raw_meta = {}
            except Exception as exc:
                raise ExtractionProviderError("VALIDATION_ERROR", "AI extractor JSON does not match schema") from exc

            latency_ms = int((time.perf_counter() - started) * 1000)
            meta = {
                "provider": raw_meta.get("provider", "remote"),
                "model": raw_meta.get("model", "unknown"),
                "latency_ms": raw_meta.get("latency_ms", latency_ms),
                "doc_coverage": raw_meta.get("doc_coverage", 1.0),
                "confidence": raw_meta.get("confidence", extracted.confidence.get("overall", 0.0)),
                "parser_version": raw_meta.get("parser_version", PARSER_VERSION),
                "warnings": raw_meta.get("warnings", []),
                "sources": raw_meta.get("sources", [str(tender_id)]),
            }
            for extra_key in (
                "chunking_version",
                "domains_extracted",
                "chunk_sizes",
                "domain_status",
                "request_count",
                "estimated_cost",
            ):
                if extra_key in raw_meta:
                    meta[extra_key] = raw_meta[extra_key]
            return ExtractionProviderResult(extracted=extracted, extract_meta=meta)

        raise ExtractionProviderError("PROVIDER_ERROR", "AI extractor service unavailable")


class ClaudeExtractorProvider(ExtractionProvider):
    async def extract(
        self,
        *,
        tender_id: UUID,
        company_id: UUID,
        tender_context: dict,
        text: str,
        chunks: dict[str, str] | None = None,
    ) -> ExtractionProviderResult:
        if not settings.ai_extractor_api_key:
            raise ExtractionProviderError("PROVIDER_ERROR", "Claude API key is not configured")

        base_url = (settings.ai_extractor_base_url or "https://api.anthropic.com").rstrip("/")
        url = f"{base_url}/v1/messages"
        model = settings.ai_extractor_model

        max_chars_total = max(1000, int(settings.ai_max_input_chars or settings.ai_extractor_max_chars))
        max_chunk_chars = min(20000, max(2000, int(max_chars_total / max(1, int(settings.ai_max_files or 10)))))
        text_limited = (text or "")[:max_chars_total]
        chunks = _chunk_text(text_limited, max_chars=max_chunk_chars)
        if not chunks:
            raise ExtractionProviderError("VALIDATION_ERROR", "No text chunks for AI extraction")
        chunks = chunks[: max(1, int(settings.ai_max_files or 10))]

        started = time.perf_counter()
        chars_sent = 0
        aggregated: dict[str, dict[str, object]] = {k: {"value": None, "confidence": 0.0, "evidence": ""} for k in _CLAUDE_FACT_FIELDS}
        request_count = 0

        for chunk in chunks:
            request_count += 1
            prompt = _build_claude_prompt(chunk)
            chars_sent += len(prompt)
            payload = {
                "model": model,
                "max_tokens": 1200,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {
                "x-api-key": settings.ai_extractor_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }

            raw_json: dict | None = None
            attempt = 0
            while True:
                attempt += 1
                try:
                    async with httpx.AsyncClient(timeout=settings.ai_extractor_timeout_sec) as client:
                        response = await client.post(url, headers=headers, json=payload)
                except httpx.TimeoutException as exc:
                    raise ExtractionProviderError("PROVIDER_TIMEOUT", "Claude timeout") from exc
                except httpx.NetworkError as exc:
                    retry_idx = attempt - 1
                    if retry_idx < len(_RETRY_NETWORK):
                        delay = _RETRY_NETWORK[retry_idx]
                        logger.warning(
                            "retry: provider=claude error=NetworkError attempt=%d delay=%.1fs",
                            attempt, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise ExtractionProviderError("PROVIDER_TIMEOUT", "Claude unreachable") from exc

                if response.status_code == 429:
                    retry_idx = attempt - 1
                    if retry_idx < len(_RETRY_RATE_LIMIT):
                        delay = _RETRY_RATE_LIMIT[retry_idx]
                        logger.warning(
                            "retry: provider=claude error=RateLimit attempt=%d delay=%.1fs",
                            attempt, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise ExtractionProviderError("PROVIDER_ERROR", "Claude rate limit exceeded")

                if response.status_code >= 500:
                    retry_idx = attempt - 1
                    if retry_idx < len(_RETRY_SERVER_ERROR):
                        delay = _RETRY_SERVER_ERROR[retry_idx]
                        logger.warning(
                            "retry: provider=claude error=ServerError(%d) attempt=%d delay=%.1fs",
                            response.status_code, attempt, delay,
                        )
                        await asyncio.sleep(delay)
                        continue
                    raise ExtractionProviderError("PROVIDER_ERROR", f"claude upstream status {response.status_code}")

                if response.status_code >= 400:
                    raise ExtractionProviderError("PROVIDER_ERROR", f"claude status {response.status_code}")

                try:
                    wire = response.json()
                except ValueError as exc:
                    raise ExtractionProviderError("VALIDATION_ERROR", "Claude returned invalid JSON envelope") from exc

                try:
                    content = wire.get("content", [])
                    text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                    joined = "\n".join(text_parts).strip()
                    raw_json = json.loads(joined)
                except Exception as exc:
                    raise ExtractionProviderError("VALIDATION_ERROR", "Claude returned non-JSON content") from exc
                break

            if raw_json is None or not isinstance(raw_json, dict):
                raise ExtractionProviderError("VALIDATION_ERROR", "Claude JSON payload missing")

            for key in _CLAUDE_FACT_FIELDS:
                item = _strict_fact_item(raw_json.get(key))
                old = aggregated[key]
                old_conf = float(old.get("confidence", 0.0) or 0.0)
                new_conf = float(item.get("confidence", 0.0) or 0.0)
                if new_conf >= old_conf:
                    aggregated[key] = item

        # Convert strict facts into existing ExtractedTenderV1 schema.
        licenses_val = aggregated["licenses"]["value"]
        licenses = licenses_val if isinstance(licenses_val, list) else []
        security_amount = _safe_int(aggregated["security_amount"]["value"])
        deadline_days = _safe_int(aggregated["deadline_days"]["value"])
        submission_deadline_at = (
            datetime.utcnow().replace(microsecond=0) + timedelta(days=deadline_days)
            if deadline_days is not None
            else None
        )

        # Map Claude aggregation keys → ExtractedTenderV1 schema field names
        # so that extraction_evidence rows use consistent keys.
        _AGG_TO_SCHEMA: dict[str, str] = {
            "security_amount": "bid_security_amount",
            "deadline_days":   "execution_days",
            "bank_guarantee":  "bank_guarantee_required",
            # sro_required, licenses, experience_required — same in both
        }
        confidence = {
            _AGG_TO_SCHEMA.get(k, k): float(v.get("confidence", 0.0) or 0.0)
            for k, v in aggregated.items()
        }
        evidence = {
            _AGG_TO_SCHEMA.get(k, k): str(v.get("evidence") or "")
            for k, v in aggregated.items()
        }

        extracted = ExtractedTenderV1(
            schema_version="v1",
            subject=str(tender_context.get("title") or "")[:500] or None,
            nmck=None,
            currency=None,
            submission_deadline_at=submission_deadline_at,
            bid_security_required=_safe_bool(aggregated["bank_guarantee"]["value"]),
            bid_security_amount=Decimal(security_amount) if security_amount is not None else None,
            bid_security_pct=None,
            contract_security_required=_safe_bool(aggregated["sro_required"]["value"]),
            contract_security_amount=None,
            contract_security_pct=None,
            qualification_requirements=[str(x) for x in licenses][:10],
            tech_parameters=[],
            penalties=[],
            confidence=confidence,
            evidence=evidence,
        )

        latency_ms = int((time.perf_counter() - started) * 1000)
        # Approximate cost meter (very rough heuristic) for visibility.
        estimated_cost = round((chars_sent / 1000.0) * 0.0008, 8)
        return ExtractionProviderResult(
            extracted=extracted,
            extract_meta={
                "provider": "claude",
                "model": model,
                "latency_ms": latency_ms,
                "chars_sent": chars_sent,
                "request_count": request_count,
                "estimated_cost": estimated_cost,
                "parser_version": PARSER_VERSION,
                "warnings": [],
                "sources": [str(tender_id)],
            },
        )


def get_extractor_provider() -> ExtractionProvider:
    mode = (settings.ai_extractor_mode or "mock").strip().lower()
    if mode == "mock":
        return MockExtractorProvider()
    if mode == "remote":
        return RemoteExtractorProvider()
    if mode == "claude":
        return ClaudeExtractorProvider()
    raise ExtractionProviderError("PROVIDER_ERROR", f"Unsupported AI_EXTRACTOR_MODE: {settings.ai_extractor_mode}")


class AIExtractorClient:
    async def extract(self, tender_id: UUID, text: str) -> ExtractedTenderV1:
        provider = get_extractor_provider()
        result = await provider.extract(
            tender_id=tender_id,
            company_id=UUID("00000000-0000-0000-0000-000000000000"),
            tender_context={},
            text=text,
            chunks=None,
        )
        return result.extracted
