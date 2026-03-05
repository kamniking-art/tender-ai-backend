from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

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
    def __init__(self, timeout_sec: int = 20, rate_limit_rps: float = 0.5) -> None:
        self.timeout_sec = timeout_sec
        self.rate_limit_rps = max(rate_limit_rps, 0.01)
        self._last_request_ts = 0.0
        self.diagnostics = SiteDiagnostics()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _record_error(self, message: str) -> None:
        self.diagnostics.error_count += 1
        if len(self.diagnostics.errors_sample) < 3:
            self.diagnostics.errors_sample.append(message)

    async def fetch_search_page(self, url: str, params: dict) -> str | None:
        backoff = [1, 3, 7]
        self.diagnostics.stage = "fetch"

        for attempt in range(len(backoff) + 1):
            await self._respect_rate_limit()
            try:
                response = await self._client.get(url, params=params)
                text = response.text or ""
                self.diagnostics.http_status = response.status_code
                self.diagnostics.fetched_bytes += len(text.encode("utf-8", errors="ignore"))
                logger.info("eis_site http: status=%s url=%s", response.status_code, str(response.url))

                if response.status_code >= 500 and attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue

                if response.status_code == 434:
                    self.diagnostics.source_status = "blocked"
                    self.diagnostics.reason = "http_434"
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
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                self.diagnostics.source_status = "error"
                self.diagnostics.reason = "timeout"
                return None
            except httpx.HTTPError as exc:
                self._record_error(exc.__class__.__name__)
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                self.diagnostics.source_status = "error"
                self.diagnostics.reason = "network_error"
                return None

        self.diagnostics.source_status = "error"
        self.diagnostics.reason = "fetch_failed"
        return None

    async def _respect_rate_limit(self) -> None:
        min_interval = 1.0 / self.rate_limit_rps
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_ts = time.monotonic()
