import asyncio
import mimetypes
import re
import time
import uuid
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
    def __init__(self, message: str, source_status: str = "error") -> None:
        super().__init__(message)
        self.source_status = source_status


_SOURCE_FETCH_GUARD_LOCK = asyncio.Lock()
_SOURCE_FETCH_LAST_CALLED_AT: dict[str, float] = {}


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
_DOC_EXT_RE = re.compile(r"\.(pdf|docx?|rtf|zip)(?:$|\?)", re.IGNORECASE)
_SOURCE_DOC_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
)


def _guess_filename_from_url(url: str, index: int) -> str:
    path_name = Path(urlparse(url).path).name
    file_name = unquote(path_name) if path_name else ""
    if not file_name or "." not in file_name:
        file_name = f"source_doc_{index}.bin"
    return sanitize_filename(file_name[:200])


async def fetch_source_documents(source_url: str, *, max_docs: int = 20) -> tuple[list[tuple[str, bytes, str | None]], str]:
    timeout = httpx.Timeout(25)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        html_text: str | None = None
        for attempt in range(3):
            try:
                page = await client.get(
                    source_url,
                    headers={
                        "User-Agent": _SOURCE_DOC_UA[attempt % len(_SOURCE_DOC_UA)],
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    },
                )
                lower = (page.text or "").lower()
                if page.status_code in {403, 434} or "captcha" in lower:
                    if attempt < 2:
                        await asyncio.sleep(1 + attempt)
                        continue
                    raise SourceFetchError("Источник временно недоступен", source_status="blocked")
                if any(marker in lower for marker in ("регламентных работ", "технических работ")):
                    raise SourceFetchError("Источник на техработах", source_status="maintenance")
                if page.status_code >= 400:
                    raise SourceFetchError(f"Ошибка источника: HTTP {page.status_code}", source_status="error")
                html_text = page.text or ""
                break
            except httpx.HTTPError:
                if attempt < 2:
                    await asyncio.sleep(1 + attempt)
                    continue
                raise SourceFetchError("Не удалось открыть страницу источника", source_status="error")

        if not html_text:
            raise SourceFetchError("Страница источника пустая", source_status="error")

        links: list[str] = []
        for href in _DOC_HREF_RE.findall(html_text):
            if not _DOC_EXT_RE.search(href):
                continue
            links.append(urljoin(source_url, href))
        links = list(dict.fromkeys(links))
        if not links:
            raise SourceFetchError("На странице не найдены ссылки на документы", source_status="ok")

        files: list[tuple[str, bytes, str | None]] = []
        total_bytes = 0
        max_total_bytes = 100 * 1024 * 1024
        for idx, link in enumerate(links[:max_docs], start=1):
            try:
                resp = await client.get(
                    link,
                    headers={"User-Agent": _SOURCE_DOC_UA[idx % len(_SOURCE_DOC_UA)]},
                )
            except httpx.HTTPError:
                continue
            if resp.status_code >= 400 or not resp.content:
                continue
            if total_bytes + len(resp.content) > max_total_bytes:
                break
            file_name = _guess_filename_from_url(link, idx)
            content_type = resp.headers.get("content-type") or mimetypes.guess_type(file_name)[0]
            files.append((file_name, resp.content, content_type))
            total_bytes += len(resp.content)

        if not files:
            raise SourceFetchError("Не удалось скачать документы из найденных ссылок", source_status="error")

        return files, "ok"


async def enforce_source_fetch_rate_limit(scope_key: str, cooldown_seconds: int = 1800) -> int | None:
    now = time.monotonic()
    async with _SOURCE_FETCH_GUARD_LOCK:
        last_called_at = _SOURCE_FETCH_LAST_CALLED_AT.get(scope_key)
        if last_called_at is not None and now - last_called_at < cooldown_seconds:
            return int(cooldown_seconds - (now - last_called_at))
        _SOURCE_FETCH_LAST_CALLED_AT[scope_key] = now
    return None
