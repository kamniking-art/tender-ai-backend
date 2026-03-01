from __future__ import annotations

from pathlib import Path
from typing import Iterable

from docx import Document as DocxDocument
from pypdf import PdfReader


class NoExtractableTextError(ValueError):
    pass


def _extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    chunks: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _extract_docx_text(path: Path) -> str:
    doc = DocxDocument(str(path))
    lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(lines)


def _extract_txt_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def extract_text_for_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
    if suffix == ".txt":
        return _extract_txt_text(path)
    return ""


def build_normalized_text(
    *,
    documents: Iterable,
    storage_root: str,
    max_chars: int,
) -> str:
    chunks: list[str] = []

    for doc in documents:
        file_path = Path(storage_root) / doc.storage_path
        if not file_path.exists() or not file_path.is_file():
            continue

        text = extract_text_for_file(file_path).strip()
        if not text:
            continue

        chunks.append(f"=== {doc.file_name} ===\n{text}\n")

    merged = "\n".join(chunks).strip()
    if len(merged) < 300:
        raise NoExtractableTextError("No extractable text")

    if len(merged) > max_chars:
        merged = merged[:max_chars] + "\n...TRUNCATED"

    return merged
