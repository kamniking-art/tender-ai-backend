from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from app.ingestion.eis_browser.parser import parse_browser_results
from app.ingestion.eis_site.parser import EISSiteCandidate

logger = logging.getLogger("uvicorn.error")

EIS_HOME_URL = "https://zakupki.gov.ru/"
EIS_SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"


@dataclass
class BrowserDiagnostics:
    stage: str = "init"
    source_status: str = "ok"
    reason: str | None = None
    pages_opened: int = 0
    found_candidates: int = 0
    error_count: int = 0
    errors_sample: list[str] = field(default_factory=list)


class EISBrowserClient:
    def __init__(self, *, state_path: str, timeout_ms: int = 20000) -> None:
        self.state_path = Path(state_path)
        self.timeout_ms = timeout_ms
        self.diagnostics = BrowserDiagnostics()

    def _record_error(self, message: str) -> None:
        self.diagnostics.error_count += 1
        if len(self.diagnostics.errors_sample) < 3:
            self.diagnostics.errors_sample.append(message)

    async def fetch_candidates(
        self,
        *,
        query: str,
        pages: int,
        page_size: int,
        limit: int,
        region: str | None = None,
    ) -> list[EISSiteCandidate]:
        self.diagnostics.stage = "browser_start"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        candidates: list[EISSiteCandidate] = []
        seen: set[str] = set()

        logger.info("eis_browser browser started: state=%s", str(self.state_path))
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context_kwargs: dict[str, Any] = {}
                if self.state_path.exists():
                    context_kwargs["storage_state"] = str(self.state_path)
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()
                page.set_default_timeout(self.timeout_ms)

                self.diagnostics.stage = "home_open"
                await page.goto(EIS_HOME_URL, wait_until="domcontentloaded")
                logger.info("eis_browser page opened: %s", EIS_HOME_URL)

                for page_number in range(1, max(1, pages) + 1):
                    if len(candidates) >= limit:
                        break
                    params = {
                        "searchString": query,
                        "pageNumber": page_number,
                        "recordsPerPage": page_size,
                        "af": "on",
                    }
                    if region:
                        params["region"] = region
                    url = f"{EIS_SEARCH_URL}?{urlencode(params)}"
                    self.diagnostics.stage = "search_submit"
                    await page.goto(url, wait_until="domcontentloaded")
                    logger.info("eis_browser search submitted: page=%s", page_number)

                    html = await page.content()
                    self.diagnostics.pages_opened += 1
                    parsed, errors = parse_browser_results(html, base_url=EIS_SEARCH_URL)
                    if errors:
                        for err in errors:
                            self._record_error(err)
                    for cand in parsed:
                        if not cand.external_id or cand.external_id in seen:
                            continue
                        seen.add(cand.external_id)
                        candidates.append(cand)
                        if len(candidates) >= limit:
                            break

                self.diagnostics.stage = "results_parsed"
                self.diagnostics.found_candidates = len(candidates)
                logger.info("eis_browser results parsed: pages=%s found=%s", self.diagnostics.pages_opened, len(candidates))

                storage_state = await context.storage_state()
                self.state_path.write_text(json.dumps(storage_state, ensure_ascii=False), encoding="utf-8")
                await context.close()
                await browser.close()
                logger.info("eis_browser browser closed")
                return candidates
        except Exception as exc:  # pragma: no cover - best-effort runtime handling
            self.diagnostics.stage = "error"
            self.diagnostics.source_status = "error"
            self.diagnostics.reason = exc.__class__.__name__
            self._record_error(str(exc))
            logger.exception("eis_browser error: %s", exc)
            return []
