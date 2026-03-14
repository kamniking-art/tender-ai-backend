from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import Page, async_playwright

from app.ingestion.eis_browser.parser import parse_browser_results
from app.ingestion.eis_site.parser import EISSiteCandidate

logger = logging.getLogger("uvicorn.error")

EIS_HOME_URL = "https://zakupki.gov.ru/"
EIS_SEARCH_URL = "https://zakupki.gov.ru/epz/order/extendedsearch/results.html"

_DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{2}:\d{2}))?")
_MONEY_RE = re.compile(r"(\d[\d\s.,]{2,})")


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
    def __init__(
        self,
        *,
        state_path: str,
        timeout_ms: int = 20000,
        debug_root: str | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.timeout_ms = timeout_ms
        self.diagnostics = BrowserDiagnostics()
        self.debug_root = Path(debug_root) if debug_root else self.state_path.parent / "debug"
        self.debug_run_dir: Path | None = None

    def _record_error(self, message: str) -> None:
        self.diagnostics.error_count += 1
        if len(self.diagnostics.errors_sample) < 5:
            self.diagnostics.errors_sample.append(message)

    @staticmethod
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

    @staticmethod
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

    def _ensure_debug_dir(self) -> Path:
        if self.debug_run_dir is None:
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            self.debug_run_dir = self.debug_root / ts
            self.debug_run_dir.mkdir(parents=True, exist_ok=True)
        return self.debug_run_dir

    async def _save_page_debug(self, page: Page, page_number: int) -> tuple[str, str]:
        debug_dir = self._ensure_debug_dir()
        html_path = debug_dir / f"page_{page_number:02d}.html"
        screenshot_path = debug_dir / f"page_{page_number:02d}.png"
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
        await page.screenshot(path=str(screenshot_path), full_page=True)
        return str(html_path), str(screenshot_path)

    async def _extract_dom_candidates(self, page: Page) -> list[EISSiteCandidate]:
        payload: list[dict[str, Any]] = await page.evaluate(
            """
            () => {
              const rows = [];
              const links = Array.from(document.querySelectorAll('a[href*="/epz/order/notice/"]'));
              const seen = new Set();
              for (const a of links) {
                const href = a.getAttribute('href') || '';
                const match = href.match(/\\b\\d{19}\\b/);
                if (!match) continue;
                const externalId = match[0];
                if (seen.has(externalId)) continue;
                seen.add(externalId);

                const card = a.closest('div.search-registry-entry-block, div.registry-entry, article, li, tr, div.row') || a.parentElement;
                const titleNode = card?.querySelector('[class*="title"], [data-qa*="title"]');
                const title = (titleNode?.textContent || a.textContent || '').trim();
                const text = (card?.textContent || '').replace(/\\s+/g, ' ').trim();
                rows.push({
                  external_id: externalId,
                  url: a.href,
                  title,
                  block_text: text,
                });
              }
              return rows;
            }
            """
        )
        out: list[EISSiteCandidate] = []
        for row in payload:
            block_text = row.get("block_text") or ""
            dates = _DATE_RE.findall(block_text)
            published_at = self._parse_datetime(" ".join(x for x in dates[0] if x)) if dates else None
            deadline = self._parse_datetime(" ".join(x for x in dates[1] if x)) if len(dates) > 1 else None
            nmck = self._parse_money(block_text)
            out.append(
                EISSiteCandidate(
                    external_id=row.get("external_id") or "",
                    title=(row.get("title") or "")[:500] or None,
                    url=row.get("url"),
                    published_at=published_at,
                    submission_deadline=deadline,
                    nmck=nmck,
                )
            )
        return [c for c in out if c.external_id]

    async def _log_selectors(self, page: Page, page_number: int) -> None:
        selectors = [
            "div.search-registry-entry-block",
            "div.registry-entry",
            "a[href*='/epz/order/notice/']",
            "a[href*='common-info.html']",
        ]
        counts: list[str] = []
        for sel in selectors:
            cnt = await page.locator(sel).count()
            counts.append(f"{sel}={cnt}")
        body_txt = (await page.locator("body").inner_text())[:600].replace("\n", " ")
        logger.info(
            "eis_browser selectors: page=%s url=%s %s",
            page_number,
            page.url,
            "; ".join(counts),
        )
        logger.info("eis_browser results text snippet: page=%s text=%s", page_number, body_txt)

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
        self._ensure_debug_dir()
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
                    logger.info("eis_browser search submitted: page=%s url=%s final_url=%s", page_number, url, page.url)

                    # Give JS-driven content time to render.
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    try:
                        await page.wait_for_selector("a[href*='/epz/order/notice/']", timeout=5000)
                    except Exception:
                        pass

                    html_path, screenshot_path = await self._save_page_debug(page, page_number)
                    logger.info(
                        "eis_browser debug artifacts: page=%s html=%s screenshot=%s",
                        page_number,
                        html_path,
                        screenshot_path,
                    )
                    await self._log_selectors(page, page_number)

                    html = await page.content()
                    self.diagnostics.pages_opened += 1

                    parsed_dom = await self._extract_dom_candidates(page)
                    parsed_html, errors = parse_browser_results(html, base_url=EIS_SEARCH_URL)
                    parsed = parsed_dom if len(parsed_dom) >= len(parsed_html) else parsed_html
                    selector_used = "dom" if parsed is parsed_dom else "html_regex"
                    logger.info(
                        "eis_browser parsed page: page=%s selector=%s dom=%s html=%s chosen=%s",
                        page_number,
                        selector_used,
                        len(parsed_dom),
                        len(parsed_html),
                        len(parsed),
                    )
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
