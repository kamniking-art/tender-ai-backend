from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
import random

import httpx

logger = logging.getLogger("uvicorn.error")

_MAINTENANCE_MARKERS = (
    "регламентных работ",
    "технической поддержки",
    "недоступен официальный сайт",
)
_BLOCKED_MARKERS = (
    "captcha",
    "доступ ограничен",
    "access denied",
    "bot protection",
)
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Connection": "keep-alive",
    "Referer": "https://zakupki.gov.ru/",
}
_HTTP_434_BACKOFF_SECONDS = (60, 180, 600)


@dataclass
class SiteDiagnostics:
    stage: str = "fetch"
    source_status: str = "ok"
    reason: str | None = None
    http_status: int | None = None
    fetched_bytes: int = 0
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)


class EISSiteClient:
    def __init__(self, timeout_sec: int = 20, min_request_delay_sec: float = 2.0, max_request_delay_sec: float = 3.5) -> None:
        self.timeout_sec = timeout_sec
        self.min_request_delay_sec = max(0.2, min_request_delay_sec)
        self.max_request_delay_sec = max(self.min_request_delay_sec, max_request_delay_sec)
        self._last_request_ts = 0.0
        self.diagnostics = SiteDiagnostics()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            follow_redirects=True,
            headers=dict(_DEFAULT_HEADERS),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _record_error(self, message: str) -> None:
        self.diagnostics.error_count += 1
        if len(self.diagnostics.errors_sample) < 3:
            self.diagnostics.errors_sample.append(message)

    async def fetch_search_page(self, url: str, params: dict, *, page_number: int | None = None) -> str | None:
        transient_backoff = [1, 3, 7]
        self.diagnostics.stage = "fetch"
        http434_seen = 0

        for attempt in range(max(len(transient_backoff), len(_HTTP_434_BACKOFF_SECONDS)) + 2):
            await self._respect_rate_limit()
            try:
                response = await self._client.get(url, params=params)
                text = response.text or ""
                self.diagnostics.http_status = response.status_code
                self.diagnostics.fetched_bytes += len(text.encode("utf-8", errors="ignore"))
                logger.info("eis_site http: status=%s url=%s", response.status_code, str(response.url))

                if response.status_code >= 500 and attempt < len(transient_backoff):
                    await asyncio.sleep(transient_backoff[attempt])
                    continue

                if response.status_code == 434:
                    if http434_seen < len(_HTTP_434_BACKOFF_SECONDS):
                        retry_in = _HTTP_434_BACKOFF_SECONDS[http434_seen]
                        logger.warning(
                            "eis_site blocked http_434 page=%s retry_in=%ss",
                            page_number if page_number is not None else "-",
                            retry_in,
                        )
                        http434_seen += 1
                        await asyncio.sleep(retry_in)
                        continue
                    self.diagnostics.source_status = "blocked"
                    self.diagnostics.reason = "http_434"
                    self._record_error("http_434")
                    return None

                if response.status_code == 403:
                    self.diagnostics.source_status = "blocked"
                    self.diagnostics.reason = "http_403"
                    self._record_error(self.diagnostics.reason)
                    if attempt < len(transient_backoff):
                        await asyncio.sleep(transient_backoff[attempt] + random.uniform(1.0, 2.0))
                        continue
                    return None

                if response.status_code >= 400:
                    self.diagnostics.source_status = "error"
                    self.diagnostics.reason = f"http_{response.status_code}"
                    self._record_error(self.diagnostics.reason)
                    return None

                lower = text.lower()
                if any(marker in lower for marker in _BLOCKED_MARKERS):
                    self.diagnostics.source_status = "blocked"
                    self.diagnostics.reason = "anti_bot"
                    return None

                if any(marker in lower for marker in _MAINTENANCE_MARKERS):
                    self.diagnostics.source_status = "maintenance"
                    self.diagnostics.reason = "maintenance_page"
                    return None

                self.diagnostics.source_status = "ok"
                self.diagnostics.reason = None
                return text
            except httpx.TimeoutException:
                self._record_error("timeout")
                if attempt < len(transient_backoff):
                    await asyncio.sleep(transient_backoff[attempt])
                    continue
                self.diagnostics.source_status = "error"
                self.diagnostics.reason = "timeout"
                return None
            except httpx.HTTPError as exc:
                self._record_error(exc.__class__.__name__)
                if attempt < len(transient_backoff):
                    await asyncio.sleep(transient_backoff[attempt])
                    continue
                self.diagnostics.source_status = "error"
                self.diagnostics.reason = "network_error"
                return None

        self.diagnostics.source_status = "error"
        self.diagnostics.reason = "fetch_failed"
        return None

    async def _respect_rate_limit(self) -> None:
        min_interval = random.uniform(self.min_request_delay_sec, self.max_request_delay_sec)
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_ts = time.monotonic()
