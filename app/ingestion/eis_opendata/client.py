from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx

from app.core.config import settings
from app.ingestion.eis_opendata.schemas import DatasetMeta, DatasetResource, DiscoveryResult, ProbeResult

logger = logging.getLogger("uvicorn.error")

_MAINTENANCE_MARKERS = ("регламентных работ", "технической поддержки", "недоступен официальный сайт")
_KNOWN_JSON_DISCOVERY_ENDPOINTS = [
    "https://data.gov.ru/portal-back/api/v1/dataset/page",
]


class EISOpenDataMaintenanceError(Exception):
    def __init__(self, reason: str, http_status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status


@dataclass
class ClientDiagnostics:
    source_status: str = "ok"
    reason: str | None = None
    catalog_url: str | None = None
    http_status: int | None = None
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)


class EISOpenDataClient:
    def __init__(
        self,
        timeout_sec: int,
        rate_limit_rps: float,
        base_url: str | None = None,
        search_page_path: str | None = None,
        search_api_url: str | None = None,
        dataset_api_url: str | None = None,
    ) -> None:
        self.timeout_sec = timeout_sec
        self.rate_limit_rps = max(rate_limit_rps, 0.01)
        self.base_url = (base_url or settings.eis_opendata_base_url).rstrip("/")
        self.search_page_path = search_page_path or settings.eis_opendata_search_path
        self.search_api_url = search_api_url or settings.eis_opendata_search_api_url
        self.dataset_api_url = dataset_api_url or settings.eis_opendata_dataset_api_url
        self._last_request_ts = 0.0
        self._discovered_search_api_url: str | None = None
        self._diagnostics = ClientDiagnostics()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_sec),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                "Referer": f"{self.base_url}/epz/main/public/home.html",
            },
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    def get_diagnostics(self) -> ClientDiagnostics:
        return self._diagnostics

    def _record_error(self, message: str) -> None:
        self._diagnostics.error_count += 1
        if len(self._diagnostics.errors_sample) < 3:
            self._diagnostics.errors_sample.append(message)

    async def discover_endpoints(self) -> DiscoveryResult:
        for endpoint in self._known_discovery_endpoints():
            probe = await self.probe_search_endpoint(endpoint)
            if probe.ok:
                logger.info("EIS_OPENDATA discovery: status=ok search_api_url=%s", endpoint)
                self._diagnostics.catalog_url = endpoint
                self._diagnostics.source_status = "ok"
                self._diagnostics.reason = None
                return DiscoveryResult(
                    status="ok",
                    search_api_url=endpoint,
                    dataset_api_url=self.dataset_api_url,
                    catalog_url=endpoint,
                    http_status=probe.http_status,
                )

        page_url = _build_page_url(self.base_url, self.search_page_path)
        self._diagnostics.catalog_url = page_url
        html, error = await self._request_text_limited(page_url, max_bytes=300 * 1024, timeout_sec=min(20, self.timeout_sec))
        if error:
            if "maintenance" in error:
                self._diagnostics.source_status = "maintenance"
                self._diagnostics.reason = "maintenance"
                return DiscoveryResult(status="maintenance", last_error=error, catalog_url=page_url, http_status=self._diagnostics.http_status)
            self._diagnostics.source_status = "error"
            self._diagnostics.reason = "catalog_unreachable"
            self._record_error(error)
            return DiscoveryResult(status="unknown", last_error=error, catalog_url=page_url, http_status=self._diagnostics.http_status)
        if not html:
            self._diagnostics.source_status = "error"
            self._diagnostics.reason = "catalog_unreachable"
            self._record_error("empty_html")
            return DiscoveryResult(status="unknown", last_error="empty_html", catalog_url=page_url, http_status=self._diagnostics.http_status)

        scripts = _extract_script_urls(html, base=page_url)[:3]
        chunks = [html]
        for script_url in scripts:
            text, _ = await self._request_text_limited(script_url, max_bytes=300 * 1024, timeout_sec=min(20, self.timeout_sec))
            if text:
                chunks.append(text)

        candidates = _extract_candidate_urls("\n".join(chunks), self.base_url)
        for endpoint in candidates:
            probe = await self.probe_search_endpoint(endpoint)
            if probe.ok:
                logger.info("EIS_OPENDATA discovery: status=ok search_api_url=%s", endpoint)
                self._diagnostics.source_status = "ok"
                self._diagnostics.reason = None
                return DiscoveryResult(
                    status="ok",
                    search_api_url=endpoint,
                    dataset_api_url=self.dataset_api_url,
                    catalog_url=page_url,
                    http_status=probe.http_status,
                )
            if probe.status == "maintenance":
                self._diagnostics.source_status = "maintenance"
                self._diagnostics.reason = "maintenance"
                return DiscoveryResult(
                    status="maintenance",
                    last_error=probe.last_error,
                    catalog_url=page_url,
                    http_status=probe.http_status,
                )

        logger.warning("EIS_OPENDATA discovery: status=unknown reason=no_working_endpoint")
        self._diagnostics.source_status = "maintenance"
        self._diagnostics.reason = "catalog_html_response"
        return DiscoveryResult(
            status="maintenance",
            last_error="catalog_html_response",
            catalog_url=page_url,
            http_status=self._diagnostics.http_status,
        )

    async def probe_search_endpoint(self, endpoint: str, q: str = "закуп", limit: int = 1) -> ProbeResult:
        response, error = await self._raw_get(endpoint, params={"q": q, "limit": limit, "offset": 0}, timeout_sec=min(20, self.timeout_sec))
        if error:
            self._record_error(error)
            return ProbeResult(ok=False, status="unknown", last_error=error, http_status=self._diagnostics.http_status)
        if response is None:
            self._record_error("no_response")
            return ProbeResult(ok=False, status="unknown", last_error="no_response", http_status=self._diagnostics.http_status)

        text = response.text or ""
        if response.status_code == 434 or _looks_like_maintenance(text):
            return ProbeResult(
                ok=False,
                status="maintenance",
                last_error=f"maintenance_http_{response.status_code}",
                http_status=response.status_code,
            )
        if response.status_code >= 400:
            self._record_error(f"http_{response.status_code}")
            return ProbeResult(ok=False, status="unknown", last_error=f"http_{response.status_code}", http_status=response.status_code)

        ctype = (response.headers.get("content-type") or "").lower()
        if "json" not in ctype and not text.lstrip().startswith(("{", "[")):
            self._record_error("non_json_response")
            return ProbeResult(ok=False, status="unknown", last_error="non_json_response", http_status=response.status_code)

        try:
            payload = response.json()
        except json.JSONDecodeError:
            self._record_error("invalid_json")
            return ProbeResult(ok=False, status="unknown", last_error="invalid_json", http_status=response.status_code)

        parsed = self._parse_datasets_from_payload(payload)
        if not parsed:
            self._record_error("no_datasets_in_payload")
            return ProbeResult(ok=False, status="unknown", last_error="no_datasets_in_payload", http_status=response.status_code)
        return ProbeResult(ok=True, status="ok", http_status=response.status_code)

    async def search_datasets(self, q: str, limit: int = 20, offset: int = 0) -> list[DatasetMeta]:
        return await self.list_datasets(q=q, limit=limit, offset=offset)

    async def list_datasets(self, q: str, limit: int = 20, offset: int = 0) -> list[DatasetMeta]:
        search_apis = [x for x in [self.search_api_url, self._discovered_search_api_url] if x]
        if not search_apis:
            discovery = await self.discover_endpoints()
            if discovery.status == "maintenance":
                raise EISOpenDataMaintenanceError(discovery.last_error or "maintenance", http_status=discovery.http_status)
            if discovery.status == "ok" and discovery.search_api_url:
                self._discovered_search_api_url = discovery.search_api_url
                search_apis.append(discovery.search_api_url)
            else:
                self._record_error(discovery.last_error or "catalog_unreachable")

        for endpoint in search_apis:
            payload = await self._query_search_endpoint(endpoint, q=q, limit=limit, offset=offset)
            if payload is None:
                continue
            datasets = self._parse_datasets_from_payload(payload)
            if datasets:
                self._discovered_search_api_url = endpoint
                return datasets[:limit]

        return []

    async def get_dataset(self, dataset_id: str) -> DatasetMeta | None:
        if dataset_id.startswith("http://") or dataset_id.startswith("https://"):
            payload = await self._request_json(dataset_id)
            if payload is not None:
                parsed = self._parse_single_dataset(payload)
                if parsed is not None:
                    return parsed
            return DatasetMeta(
                dataset_id=dataset_id,
                title="Known dataset fallback",
                updated_at=None,
                resources=[DatasetResource(url=dataset_id, name=dataset_id.rsplit("/", 1)[-1] or "dataset.bin")],
            )

        candidate_endpoints = [self.dataset_api_url, self._discovered_search_api_url, self.search_api_url]
        for endpoint in [x for x in candidate_endpoints if x]:
            payload = await self._query_dataset_endpoint(endpoint, dataset_id)
            if payload is None:
                continue
            dataset = self._parse_single_dataset(payload, expected_dataset_id=dataset_id)
            if dataset is not None:
                return dataset

        found = await self.list_datasets(q=dataset_id, limit=20, offset=0)
        for ds in found:
            if ds.dataset_id == dataset_id:
                return ds
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

    async def _request_text_limited(self, url: str, max_bytes: int, timeout_sec: int) -> tuple[str | None, str | None]:
        response, error = await self._raw_get(url, params=None, timeout_sec=timeout_sec)
        if error:
            self._record_error(error)
            return None, error
        if response is None:
            self._record_error("no_response")
            return None, "no_response"

        body = response.text or ""
        if response.status_code == 434 or _looks_like_maintenance(body):
            return None, f"maintenance_http_{response.status_code}"
        if response.status_code >= 400:
            self._record_error(f"http_{response.status_code}")
            return None, f"http_{response.status_code}"
        if len(body.encode("utf-8", errors="ignore")) > max_bytes:
            body = body[: max_bytes // 2]
        return body, None

    async def _raw_get(self, url: str, params: dict | None, timeout_sec: int) -> tuple[httpx.Response | None, str | None]:
        backoff = [1, 3, 7]
        for attempt in range(len(backoff) + 1):
            await self._respect_rate_limit()
            try:
                response = await self._client.get(url, params=params, timeout=timeout_sec)
                logger.info("eis_opendata http: status=%s url=%s", response.status_code, str(response.url))
                self._diagnostics.http_status = response.status_code
                self._diagnostics.catalog_url = str(response.url)
                if response.status_code >= 500 and attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                return response, None
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                self._record_error("timeout_or_network")
                return None, "timeout_or_network"
            except httpx.HTTPError:
                if attempt < len(backoff):
                    await asyncio.sleep(backoff[attempt])
                    continue
                self._record_error("http_error")
                return None, "http_error"
        return None, "unknown_error"

    async def _query_search_endpoint(self, endpoint: str, q: str, limit: int, offset: int) -> dict | list | None:
        variants = [
            {"q": q, "limit": limit, "offset": offset},
            {"query": q, "limit": limit, "offset": offset},
            {"search": q, "size": limit, "number": max(0, offset // max(limit, 1))},
            {"searchString": q, "pageNumber": max(1, offset // max(limit, 1) + 1), "recordsPerPage": limit},
            {"text": q, "page": max(1, offset // max(limit, 1) + 1), "size": limit},
        ]
        for params in variants:
            payload = await self._request_json(endpoint, params=params)
            if payload is None:
                continue
            if self._parse_datasets_from_payload(payload):
                return payload
        return None

    async def _query_dataset_endpoint(self, endpoint: str, dataset_id: str) -> dict | list | None:
        url = endpoint
        if "{id}" in endpoint:
            url = endpoint.replace("{id}", dataset_id)
            return await self._request_json(url)

        variants = [{"id": dataset_id}, {"datasetId": dataset_id}, {"dataset_id": dataset_id}, {"q": dataset_id}]
        for params in variants:
            payload = await self._request_json(url, params=params)
            if payload is not None:
                return payload
        return None

    async def _request_json(self, url: str, params: dict | None = None) -> dict | list | None:
        response, _ = await self._raw_get(url, params=params, timeout_sec=self.timeout_sec)
        if response is None:
            self._record_error("no_response")
            return None

        text = response.text or ""
        if response.status_code == 434 or _looks_like_maintenance(text):
            raise EISOpenDataMaintenanceError("maintenance", http_status=response.status_code)
        if response.status_code >= 400:
            self._record_error(f"http_{response.status_code}")
            return None

        ctype = (response.headers.get("content-type") or "").lower()
        if "json" not in ctype and not text.lstrip().startswith(("{", "[")):
            self._record_error("non_json_response")
            return None
        try:
            return response.json()
        except json.JSONDecodeError:
            self._record_error("invalid_json")
            return None

    async def _respect_rate_limit(self) -> None:
        min_interval = 1.0 / self.rate_limit_rps
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        self._last_request_ts = time.monotonic()

    def _parse_datasets_from_payload(self, payload: dict | list) -> list[DatasetMeta]:
        items = _extract_items(payload)
        datasets: list[DatasetMeta] = []
        for item in items:
            parsed = self._parse_dataset(item)
            if parsed is not None:
                datasets.append(parsed)
        return datasets

    def _parse_single_dataset(self, payload: dict | list, expected_dataset_id: str | None = None) -> DatasetMeta | None:
        if isinstance(payload, dict):
            direct = self._parse_dataset(payload)
            if direct and (expected_dataset_id is None or direct.dataset_id == expected_dataset_id):
                return direct

        for item in _extract_items(payload):
            parsed = self._parse_dataset(item)
            if parsed is None:
                continue
            if expected_dataset_id is None or parsed.dataset_id == expected_dataset_id:
                return parsed
        return None

    def _parse_dataset(self, raw: object) -> DatasetMeta | None:
        if not isinstance(raw, dict):
            return None

        dataset_id = str(
            raw.get("dataset_id")
            or raw.get("datasetId")
            or raw.get("id")
            or raw.get("identifier")
            or raw.get("code")
            or raw.get("name")
            or ""
        ).strip()
        if not dataset_id:
            return None

        title_obj = raw.get("title") or raw.get("name") or raw.get("datasetTitle")
        title = title_obj if isinstance(title_obj, str) else None

        updated_at = _parse_dt(
            raw.get("updated_at")
            or raw.get("updatedAt")
            or raw.get("lastChangeDate")
            or raw.get("modified")
            or raw.get("metadata_modified")
            or raw.get("lastUpdate")
        )

        resources: list[DatasetResource] = []
        raw_resources = None
        for key in ("files", "resources", "attachments", "downloads", "data"):
            if isinstance(raw.get(key), list):
                raw_resources = raw.get(key)
                break

        if raw_resources is None:
            raw_resources = []

        for res in raw_resources:
            if not isinstance(res, dict):
                continue
            url = res.get("url") or res.get("downloadUrl") or res.get("href") or res.get("source")
            if not isinstance(url, str) or not url:
                continue
            resources.append(
                DatasetResource(
                    url=urljoin(self.base_url + "/", url),
                    name=(res.get("name") if isinstance(res.get("name"), str) else None),
                    updated_at=_parse_dt(
                        res.get("updated_at") or res.get("updatedAt") or res.get("modified") or res.get("last_modified")
                    ),
                    version=(str(res.get("version")) if res.get("version") is not None else None),
                    size=_parse_int(res.get("size") or res.get("fileSize")),
                    format=(res.get("format") if isinstance(res.get("format"), str) else None),
                )
            )

        return DatasetMeta(dataset_id=dataset_id, title=title, updated_at=updated_at, resources=resources)

    def _known_discovery_endpoints(self) -> list[str]:
        endpoints: list[str] = []
        for endpoint in [self.search_api_url, self.dataset_api_url, *_KNOWN_JSON_DISCOVERY_ENDPOINTS]:
            if isinstance(endpoint, str) and endpoint and endpoint not in endpoints:
                endpoints.append(endpoint)
        return endpoints


def _build_page_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return urljoin(base_url + "/", path.lstrip("/"))


def _extract_script_urls(html: str, base: str) -> list[str]:
    urls = re.findall(r'src=["\']([^"\']+\.js[^"\']*)["\']', html, flags=re.IGNORECASE)
    return [urljoin(base, x) for x in urls]


def _extract_candidate_urls(content: str, base_url: str) -> list[str]:
    candidates: set[str] = set()

    for match in re.findall(r'https?://[^"\'\s)]+', content):
        if any(token in match.lower() for token in ("opendata", "dataset", "/api/", "search", "download")):
            candidates.add(match)

    for match in re.findall(r'/(?:epz/)?[^"\'\s)]+', content):
        lower = match.lower()
        if any(token in lower for token in ("opendata", "dataset", "/api/", "search", "download")):
            if lower.endswith((".js", ".css", ".svg", ".png", ".jpg")):
                continue
            candidates.add(urljoin(base_url + "/", match.lstrip("/")))

    return sorted(candidates)


def _extract_items(payload: dict | list) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    queue: list[object] = [payload]
    while queue:
        current = queue.pop(0)
        if isinstance(current, list):
            dict_items = [x for x in current if isinstance(x, dict)]
            if dict_items:
                return dict_items
            queue.extend(current)
            continue
        if isinstance(current, dict):
            for key in ("results", "items", "content", "data", "datasets", "list"):
                val = current.get(key)
                if isinstance(val, list):
                    dict_items = [x for x in val if isinstance(x, dict)]
                    if dict_items:
                        return dict_items
                elif isinstance(val, dict):
                    queue.append(val)
    return []


def _looks_like_maintenance(body: str) -> bool:
    low = body.lower()
    return any(marker in low for marker in _MAINTENANCE_MARKERS)


def _parse_dt(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OSError):
            return None
    if not isinstance(value, str):
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


def _parse_int(value: object) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
