from __future__ import annotations

from app.ingestion.eis_site.parser import EISSiteCandidate, parse_search_page


def parse_browser_results(html_text: str, base_url: str) -> tuple[list[EISSiteCandidate], list[str]]:
    parsed = parse_search_page(html_text, base_url=base_url)
    return parsed.candidates, parsed.errors
