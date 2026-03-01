from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.ingestion.eis_opendata.schemas import DatasetMeta, DatasetResource

logger = logging.getLogger("uvicorn.error")

DEFAULT_PACKAGE_SEARCH_URL = "https://zakupki.gov.ru/opendata/api/3/action/package_search"
DEFAULT_PACKAGE_SHOW_URL = "https://zakupki.gov.ru/opendata/api/3/action/package_show"


class EISOpenDataClient:
    def __init__(
        self,
        timeout_sec: int,
        rate_limit_rps: float,
        package_search_url: str = DEFAULT_PACKAGE_SEARCH_URL,
        package_show_url: str = DEFAULT_PACKAGE_SHOW_URL,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.rate_limit_rps = max(rate_limit_rps, 0.01)
        self.package_search_url = package_search_url
        self.package_show_url = package_show_url
        self._last_request_ts = 0.0
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def list_datasets(self, q: str, limit: int = 20) -> list[DatasetMeta]:
        payload = await self._request_json(self.package_search_url, params={"q": q, "rows": max(1, min(100, limit))})
        if not isinstance(payload, dict):
            return []

        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        raw_results = result.get("results") if isinstance(result, dict) else []
        if not isinstance(raw_results, list):
            return []

        datasets: list[DatasetMeta] = []
        for raw in raw_results:
            parsed = self._parse_dataset(raw)
            if parsed is not None:
                datasets.append(parsed)
            if len(datasets) >= limit:
                break
        return datasets

    async def get_dataset(self, dataset_id: str) -> DatasetMeta | None:
        params = {"id": dataset_id}
        payload = await self._request_json(self.package_show_url, params=params)
        if isinstance(payload, dict):
            result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
            parsed = self._parse_dataset(result)
            if parsed is not None:
                return parsed

        if dataset_id.startswith("http://") or dataset_id.startswith("https://"):
            payload = await self._request_json(dataset_id)
            if isinstance(payload, dict):
                result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
                return self._parse_dataset(result)

        return None

    async def download_to(self, url: str, destination: Path) -> bool:
        backoff = [1, 3, 7]
        for attempt in range(len(backoff) + 1):
            await self._respect_rate_limit()
            try:
                async with self._client.stream("GET", url) as response:
                    logger.info("eis_opendata http: status=%s url=%s", response.status_code, str(response.url))
                    if response.status_code >= 500 and attempt < len(backoff):
                        await asyncio.sleep(backoff[attempt])
                        continue
                    if response.status_code >= 400:
                        return False

                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with destination.open("wb") as fh:
                        async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                            fh.write(chunk)
                    return True
            except (httpx.TimeoutException, httpx.NetworkError):
                logger.warning("eis_opendata timeout/network error: url=%s attempt=%s", url, attempt + 1)
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                return False
            except httpx.HTTPError:
                logger.exception("eis_opendata http error: url=%s", url)
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                return False
        return False

    async def _request_json(self, url: str, params: dict | None = None) -> dict | list | None:
        backoff = [1, 3, 7]
        for attempt in range(len(backoff) + 1):
            await self._respect_rate_limit()
            try:
                response = await self._client.get(url, params=params)
                logger.info("eis_opendata http: status=%s url=%s", response.status_code, str(response.url))

                if response.status_code >= 500 and attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                if response.status_code >= 400:
                    return None

                ctype = (response.headers.get("content-type") or "").lower()
                if "json" not in ctype and not response.text.lstrip().startswith(("{", "[")):
                    logger.warning("eis_opendata non-json metadata: url=%s content-type=%s", str(response.url), ctype)
                    return None
                return response.json()
            except json.JSONDecodeError:
                logger.warning("eis_opendata invalid json: url=%s", url)
                return None
            except (httpx.TimeoutException, httpx.NetworkError):
                logger.warning("eis_opendata timeout/network error: url=%s attempt=%s", url, attempt + 1)
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                return None
            except httpx.HTTPError:
                logger.exception("eis_opendata http error: url=%s", url)
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

    def _parse_dataset(self, raw: object) -> DatasetMeta | None:
        if not isinstance(raw, dict):
            return None

        dataset_id = str(raw.get("id") or raw.get("identifier") or raw.get("name") or "").strip()
        if not dataset_id:
            return None

        title = (raw.get("title") or raw.get("title_translated") or raw.get("name") or None)
        if isinstance(title, dict):
            title = title.get("ru") or title.get("en") or None

        updated_at = _parse_dt(
            raw.get("metadata_modified")
            or raw.get("revision_timestamp")
            or raw.get("updated_at")
            or raw.get("modified")
        )

        resources: list[DatasetResource] = []
        raw_resources = raw.get("resources") if isinstance(raw.get("resources"), list) else []
        for res in raw_resources:
            if not isinstance(res, dict):
                continue
            url = res.get("url") or res.get("download_url")
            if not isinstance(url, str) or not url:
                continue
            resources.append(
                DatasetResource(
                    url=url,
                    name=(res.get("name") if isinstance(res.get("name"), str) else None),
                    updated_at=_parse_dt(
                        res.get("last_modified")
                        or res.get("created")
                        or res.get("updated")
                        or res.get("metadata_modified")
                    ),
                    version=(str(res.get("revision_id")) if res.get("revision_id") else None),
                )
            )

        return DatasetMeta(dataset_id=dataset_id, title=title if isinstance(title, str) else None, updated_at=updated_at, resources=resources)


def _parse_dt(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None
