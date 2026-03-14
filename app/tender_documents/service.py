import asyncio
import mimetypes
import random
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from uuid import UUID

import httpx
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.tender_documents.model import TenderDocument
from app.tenders.model import Tender


class ScopedNotFoundError(Exception):
    pass


class DocumentStorageError(Exception):
    pass


class SourceFetchError(Exception):
    def __init__(
        self,
        message: str,
        source_status: str = "error",
        *,
        found_links_count: int = 0,
        attempted_pages: int = 0,
        http_status: int | None = None,
        errors_sample: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.source_status = source_status
        self.found_links_count = found_links_count
        self.attempted_pages = attempted_pages
        self.http_status = http_status
        self.errors_sample = errors_sample or []


@dataclass
class SourceFetchedFile:
    file_name: str
    content: bytes
    content_type: str | None
    source_link: str


@dataclass
class SourceFetchResult:
    source_status: str
    message: str
    attempted_pages: int
    found_links_count: int
    http_status: int | None
    files: list[SourceFetchedFile]
    errors_sample: list[str]


_SOURCE_FETCH_GUARD_LOCK = asyncio.Lock()
_SOURCE_FETCH_LAST_CALLED_AT: dict[str, float] = {}
_SOURCE_FETCH_BLOCKED_UNTIL: dict[str, float] = {}


def sanitize_filename(filename: str | None) -> str:
    candidate = Path(filename or "").name
    sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", candidate).strip("._")
    return sanitized or "file"


def build_storage_path(company_id: UUID, tender_id: UUID, document_id: UUID, original_filename: str | None) -> tuple[str, Path]:
    safe_name = sanitize_filename(original_filename)
    relative_path = Path(settings.documents_subdir) / str(company_id) / str(tender_id) / f"{document_id}_{safe_name}"
    absolute_path = Path(settings.storage_root) / relative_path
    return relative_path.as_posix(), absolute_path


async def _get_tender_scoped(db: AsyncSession, company_id: UUID, tender_id: UUID) -> Tender | None:
    stmt = select(Tender).where(Tender.id == tender_id, Tender.company_id == company_id)
    return await db.scalar(stmt)


async def create_document_for_tender(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    uploaded_by: UUID,
    file: UploadFile,
    doc_type: str | None,
) -> TenderDocument:
    tender = await _get_tender_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    try:
        content = await file.read()
    finally:
        await file.close()

    return await create_document_from_bytes(
        db,
        company_id=company_id,
        tender_id=tender_id,
        uploaded_by=uploaded_by,
        file_name=file.filename or "file",
        content=content,
        content_type=file.content_type,
        doc_type=doc_type,
    )


def write_bytes_to_storage(relative_path: str, content: bytes) -> Path:
    absolute_path = Path(settings.storage_root) / relative_path
    try:
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        absolute_path.write_bytes(content)
    except OSError as exc:
        raise DocumentStorageError("Failed to store file") from exc
    return absolute_path


async def create_document_from_bytes(
    db: AsyncSession,
    *,
    company_id: UUID,
    tender_id: UUID,
    uploaded_by: UUID | None,
    file_name: str,
    content: bytes,
    content_type: str | None,
    doc_type: str | None,
    relative_path_override: str | None = None,
) -> TenderDocument:
    tender = await _get_tender_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    document_id = uuid.uuid4()
    if relative_path_override:
        relative_path = relative_path_override
    else:
        relative_path, _ = build_storage_path(company_id, tender_id, document_id, file_name)

    absolute_path = write_bytes_to_storage(relative_path, content)

    document = TenderDocument(
        id=document_id,
        company_id=company_id,
        tender_id=tender_id,
        file_name=file_name,
        storage_path=relative_path,
        content_type=content_type,
        doc_type=doc_type,
        file_size=len(content),
        uploaded_by=uploaded_by,
    )

    db.add(document)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        try:
            if absolute_path.exists():
                absolute_path.unlink()
        except OSError:
            pass
        raise

    await db.refresh(document)
    return document


async def list_documents_for_tender(db: AsyncSession, *, company_id: UUID, tender_id: UUID) -> list[TenderDocument]:
    tender = await _get_tender_scoped(db, company_id, tender_id)
    if tender is None:
        raise ScopedNotFoundError("Tender not found")

    stmt = (
        select(TenderDocument)
        .where(TenderDocument.company_id == company_id, TenderDocument.tender_id == tender_id)
        .order_by(TenderDocument.uploaded_at.desc())
    )
    return list((await db.scalars(stmt)).all())


async def get_document_scoped(db: AsyncSession, *, company_id: UUID, document_id: UUID) -> TenderDocument | None:
    stmt = select(TenderDocument).where(TenderDocument.id == document_id, TenderDocument.company_id == company_id)
    return await db.scalar(stmt)


async def delete_document_scoped(db: AsyncSession, *, company_id: UUID, document_id: UUID) -> bool:
    document = await get_document_scoped(db, company_id=company_id, document_id=document_id)
    if document is None:
        raise ScopedNotFoundError("Document not found")

    file_path = Path(settings.storage_root) / document.storage_path

    await db.delete(document)
    await db.commit()

    if file_path.exists():
        try:
            file_path.unlink()
        except OSError as exc:
            raise DocumentStorageError("Document deleted from DB, but failed to delete file") from exc

    return True


_DOC_HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.IGNORECASE)
_DOC_EXT_RE = re.compile(r"\.(pdf|docx?|xlsx?|zip|rar)(?:$|\?)", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b([^>]+)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_ATTR_LINK_RE = re.compile(
    r'(?:href|data-href|data-url|data-link|src)\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_ONCLICK_LINK_RE = re.compile(
    r"""(?:open|location(?:\.href)?|window\.open)\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE,
)
_RAW_JS_LINK_RE = re.compile(
    r"""['"]((?:https?://|/)[^'"]+(?:\.(?:pdf|docx?|xlsx?|zip|rar)|documents?|attachments?)[^'"]*)['"]""",
    re.IGNORECASE,
)
_DOC_PAGE_KEYWORDS = (
    "document",
    "documents",
    "docs",
    "attachment",
    "attachments",
    "влож",
    "документ",
    "документац",
    "документация",
    "файл",
    "скачат",
    "прикреп",
)
_SOURCE_DOC_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
)
_ALLOWED_DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar"}


async def _paced_delay() -> None:
    base = max(0.2, float(settings.eis_source_request_delay_sec))
    jitter = max(0.0, float(settings.eis_source_request_jitter_sec))
    await asyncio.sleep(base + random.uniform(0.0, jitter))


def _blocked_retry_after_seconds(source_url: str) -> int | None:
    until = _SOURCE_FETCH_BLOCKED_UNTIL.get(source_url)
    if until is None:
        return None
    delta = int(until - time.monotonic())
    if delta <= 0:
        _SOURCE_FETCH_BLOCKED_UNTIL.pop(source_url, None)
        return None
    return delta


def _set_blocked_cooldown(source_url: str) -> int:
    cooldown = max(5 * 60, int(settings.eis_source_blocked_cooldown_minutes * 60))
    _SOURCE_FETCH_BLOCKED_UNTIL[source_url] = time.monotonic() + cooldown
    return cooldown


def _guess_filename_from_url(url: str, index: int) -> str:
    path_name = Path(urlparse(url).path).name
    file_name = unquote(path_name) if path_name else ""
    if not file_name or "." not in file_name:
        file_name = f"source_doc_{index}.bin"
    return sanitize_filename(file_name[:200])


def _normalize_link(base_url: str, link: str) -> str | None:
    raw = (link or "").strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "#")):
        return None
    return urljoin(base_url, raw)


def _is_doc_link(link: str) -> bool:
    path = urlparse(link).path.lower()
    ext = Path(path).suffix.lower()
    return ext in _ALLOWED_DOC_EXTENSIONS


def _clean_html_text(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", value or "", flags=re.DOTALL)
    return " ".join(no_tags.split()).strip().lower()


def _extract_candidate_links(base_url: str, html_text: str) -> tuple[list[str], list[str]]:
    doc_links: list[str] = []
    page_links: list[str] = []

    for link in _ATTR_LINK_RE.findall(html_text):
        full = _normalize_link(base_url, link)
        if not full:
            continue
        if _is_doc_link(full):
            doc_links.append(full)
        elif any(token in full.lower() for token in _DOC_PAGE_KEYWORDS):
            page_links.append(full)

    for link in _DOC_HREF_RE.findall(html_text):
        full = _normalize_link(base_url, link)
        if not full:
            continue
        if _is_doc_link(full):
            doc_links.append(full)

    for attrs, inner_html in _ANCHOR_RE.findall(html_text):
        text = _clean_html_text(inner_html)
        href_match = _ATTR_LINK_RE.search(attrs)
        href = href_match.group(1) if href_match else ""
        full = _normalize_link(base_url, href)
        if not full:
            continue
        if _is_doc_link(full):
            doc_links.append(full)
            continue
        if any(token in text or token in full.lower() for token in _DOC_PAGE_KEYWORDS):
            page_links.append(full)

    for link in _ONCLICK_LINK_RE.findall(html_text):
        full = _normalize_link(base_url, link)
        if not full:
            continue
        if _is_doc_link(full):
            doc_links.append(full)
        elif any(token in full.lower() for token in _DOC_PAGE_KEYWORDS):
            page_links.append(full)

    for link in _RAW_JS_LINK_RE.findall(html_text):
        full = _normalize_link(base_url, link)
        if not full:
            continue
        if _is_doc_link(full):
            doc_links.append(full)
        elif any(token in full.lower() for token in _DOC_PAGE_KEYWORDS):
            page_links.append(full)

    return list(dict.fromkeys(doc_links)), list(dict.fromkeys(page_links))


def _guess_related_document_pages(source_url: str) -> list[str]:
    parsed = urlparse(source_url)
    path = parsed.path or ""
    candidates = [source_url]
    if "common-info" in path:
        candidates.append(source_url.replace("common-info", "documents-info"))
        candidates.append(source_url.replace("common-info", "docs"))
        candidates.append(source_url.replace("common-info", "attachments"))
    candidates.append(urljoin(source_url, "./documents.html"))
    candidates.append(urljoin(source_url, "./docs.html"))
    return list(dict.fromkeys(candidates))


async def fetch_source_documents(source_url: str, *, max_docs: int = 20) -> SourceFetchResult:
    blocked_retry = _blocked_retry_after_seconds(source_url)
    if blocked_retry is not None:
        raise SourceFetchError(
            "ЕИС временно блокирует запросы (HTTP 434), попробуйте позже",
            source_status="blocked",
            attempted_pages=0,
            http_status=434,
            errors_sample=[f"cooldown_active_retry_after={blocked_retry}s"],
        )

    timeout = httpx.Timeout(25)
    errors: list[str] = []

    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "User-Agent": _SOURCE_DOC_UA[0],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": "https://zakupki.gov.ru/epz/main/public/home.html",
            "Connection": "keep-alive",
        },
    ) as client:
        html_text: str | None = None
        attempted_pages = 0
        last_http_status: int | None = None
        related_pages_to_try: list[str] = []
        all_doc_links: list[str] = []
        visited_page_links: set[str] = set()

        async def fetch_html(page_url: str, retries: int = 2) -> str:
            nonlocal attempted_pages
            attempted_pages += 1
            for attempt in range(retries + 1):
                try:
                    await _paced_delay()
                    page = await client.get(
                        page_url,
                        headers={
                            "User-Agent": _SOURCE_DOC_UA[attempt % len(_SOURCE_DOC_UA)],
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                            "Referer": "https://zakupki.gov.ru/epz/main/public/home.html",
                        },
                    )
                    last_http_status = page.status_code
                    lower = (page.text or "").lower()
                    if page.status_code in {403, 429, 434} or "captcha" in lower:
                        if attempt < retries:
                            await _paced_delay()
                            continue
                        if page.status_code == 434:
                            _set_blocked_cooldown(source_url)
                        raise SourceFetchError(
                            "ЕИС временно блокирует запросы (HTTP 434), попробуйте позже"
                            if page.status_code == 434
                            else "Источник временно недоступен",
                            source_status="blocked",
                            attempted_pages=attempted_pages,
                            http_status=page.status_code,
                            errors_sample=errors[:3],
                        )
                    if any(marker in lower for marker in ("регламентных работ", "технических работ")):
                        raise SourceFetchError(
                            "Источник на техработах",
                            source_status="maintenance",
                            attempted_pages=attempted_pages,
                            http_status=page.status_code,
                            errors_sample=errors[:3],
                        )
                    if page.status_code >= 400:
                        if attempt < retries:
                            await _paced_delay()
                            continue
                        raise SourceFetchError(
                            f"Ошибка источника: HTTP {page.status_code}",
                            source_status="error",
                            attempted_pages=attempted_pages,
                            http_status=page.status_code,
                            errors_sample=errors[:3],
                        )
                    return page.text or ""
                except httpx.HTTPError as exc:
                    errors.append(f"{page_url}: {exc.__class__.__name__}")
                    if attempt < retries:
                        await _paced_delay()
                        continue
                    raise SourceFetchError(
                        "Не удалось открыть страницу источника",
                        source_status="error",
                        attempted_pages=attempted_pages,
                        http_status=last_http_status,
                        errors_sample=errors[:3],
                    ) from exc
            return ""

        for attempt in range(3):
            try:
                html_text = await fetch_html(source_url)
                break
            except SourceFetchError:
                if attempt < 2:
                    await _paced_delay()
                    continue
                raise

        if not html_text:
            raise SourceFetchError(
                "Страница источника пустая",
                source_status="error",
                attempted_pages=attempted_pages,
                http_status=last_http_status,
                errors_sample=errors[:3],
            )

        main_doc_links, page_links = _extract_candidate_links(source_url, html_text)
        all_doc_links.extend(main_doc_links)
        related_pages_to_try.extend(page_links)
        related_pages_to_try.extend(_guess_related_document_pages(source_url))

        max_related_pages = 5
        for page_link in related_pages_to_try:
            if len(visited_page_links) >= max_related_pages:
                break
            normalized = _normalize_link(source_url, page_link)
            if not normalized or normalized in visited_page_links:
                continue
            visited_page_links.add(normalized)
            if normalized == source_url:
                continue
            try:
                page_html = await fetch_html(normalized, retries=1)
            except SourceFetchError as exc:
                if exc.source_status in {"blocked", "maintenance"}:
                    raise
                errors.append(f"{normalized}: {exc}")
                continue
            doc_links, _ = _extract_candidate_links(normalized, page_html)
            all_doc_links.extend(doc_links)

        unique_links = list(dict.fromkeys(link for link in all_doc_links if _DOC_EXT_RE.search(link)))
        found_links_count = len(unique_links)
        if not unique_links:
            raise SourceFetchError(
                "На карточке ЕИС документы не найдены",
                source_status="ok",
                found_links_count=0,
                attempted_pages=attempted_pages,
                http_status=last_http_status,
                errors_sample=errors[:3],
            )

        files: list[SourceFetchedFile] = []
        seen_download_signatures: set[tuple[str, int]] = set()
        total_bytes = 0
        max_total_bytes = 100 * 1024 * 1024
        for idx, link in enumerate(unique_links[:max_docs], start=1):
            try:
                await _paced_delay()
                resp = await client.get(
                    link,
                    headers={"User-Agent": _SOURCE_DOC_UA[idx % len(_SOURCE_DOC_UA)]},
                )
            except httpx.HTTPError as exc:
                errors.append(f"{link}: {exc.__class__.__name__}")
                continue
            if resp.status_code >= 400 or not resp.content:
                errors.append(f"{link}: http_{resp.status_code}")
                continue
            if total_bytes + len(resp.content) > max_total_bytes:
                break
            file_name = _guess_filename_from_url(link, idx)
            signature = (file_name.lower(), len(resp.content))
            if signature in seen_download_signatures:
                continue
            content_type = resp.headers.get("content-type") or mimetypes.guess_type(file_name)[0]
            files.append(SourceFetchedFile(file_name=file_name, content=resp.content, content_type=content_type, source_link=link))
            seen_download_signatures.add(signature)
            total_bytes += len(resp.content)

        if not files:
            raise SourceFetchError(
                "Не удалось скачать документы из найденных ссылок",
                source_status="error",
                found_links_count=found_links_count,
                attempted_pages=attempted_pages,
                http_status=last_http_status,
                errors_sample=errors[:3],
            )

        return SourceFetchResult(
            source_status="ok",
            message="Документы загружены",
            attempted_pages=attempted_pages,
            found_links_count=found_links_count,
            http_status=last_http_status,
            files=files,
            errors_sample=errors[:3],
        )


async def enforce_source_fetch_rate_limit(scope_key: str, cooldown_seconds: int = 1800) -> int | None:
    now = time.monotonic()
    async with _SOURCE_FETCH_GUARD_LOCK:
        last_called_at = _SOURCE_FETCH_LAST_CALLED_AT.get(scope_key)
        if last_called_at is not None and now - last_called_at < cooldown_seconds:
            return int(cooldown_seconds - (now - last_called_at))
        _SOURCE_FETCH_LAST_CALLED_AT[scope_key] = now
    return None
