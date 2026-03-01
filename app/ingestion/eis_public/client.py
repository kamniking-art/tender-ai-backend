import asyncio
import logging
import time

import httpx

logger = logging.getLogger("uvicorn.error")


class EISPublicClient:
    def __init__(self, timeout_sec: int = 20, rate_limit_rps: float = 0.5) -> None:
        self.timeout_sec = timeout_sec
        self.rate_limit_rps = max(rate_limit_rps, 0.01)
        self._last_request_ts = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def get_text(self, url: str, params: dict | None = None) -> str | None:
        backoff = [1, 3, 7]
        for attempt in range(len(backoff) + 1):
            await self._respect_rate_limit()
            try:
                response = await self._client.get(url, params=params)
                logger.info("ingestion http: status=%s url=%s", response.status_code, str(response.url))

                if response.status_code >= 500 and attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                if response.status_code >= 400:
                    return None

                return response.text
            except httpx.TimeoutException:
                logger.warning("ingestion timeout: url=%s attempt=%s", url, attempt + 1)
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                return None
            except httpx.HTTPError:
                logger.exception("ingestion http error: url=%s", url)
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                return None
        return None

    async def _respect_rate_limit(self) -> None:
        min_interval = 1.0 / self.rate_limit_rps
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_ts = time.monotonic()
