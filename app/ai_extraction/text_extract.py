from __future__ import annotations

import logging
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable
from zipfile import ZipFile

from docx import Document as DocxDocument
from pypdf import PdfReader

logger = logging.getLogger(__name__)


class NoExtractableTextError(ValueError):
    pass


def _extract_pdf_text(path: Path, *, max_pages: int | None = None) -> str:
    reader = PdfReader(str(path))
    chunks: list[str] = []
    pages = reader.pages if max_pages is None else reader.pages[: max(1, max_pages)]
    for page in pages:
        text = page.extract_text() or ""
        if text:
            chunks.append(text)
    return "\n".join(chunks)


def _extract_docx_text(path: Path) -> str:
    doc = DocxDocument(str(path))
    lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    return "\n".join(lines)


def _extract_doc_text(path: Path) -> str:
    """Extract text from legacy .doc files using antiword."""
    try:
        result = subprocess.run(
            ["antiword", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        # antiword failed — try reading as plain text fallback
        logger.warning("antiword failed for %s: %s", path, result.stderr)
        return ""
    except FileNotFoundError:
        logger.warning("antiword not installed, cannot read .doc file: %s", path)
        return ""
    except subprocess.TimeoutExpired:
        logger.warning("antiword timeout for %s", path)
        return ""
    except Exception as exc:
        logger.warning("doc extraction error for %s: %s", path, exc)
        return ""


def _extract_txt_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _extract_xlsx_text(path: Path) -> str:
    try:
        with ZipFile(path) as zf:
            names = set(zf.namelist())
            shared_strings: list[str] = []

            if "xl/sharedStrings.xml" in names:
                with zf.open("xl/sharedStrings.xml") as fp:
                    tree = ET.parse(fp)
                for si in tree.getroot().iter():
                    tag = si.tag.split("}")[-1]
                    if tag == "t" and si.text:
                        shared_strings.append(si.text.strip())

            lines: list[str] = []
            sheet_names = sorted(
                name for name in names if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )
            for sheet_name in sheet_names:
                with zf.open(sheet_name) as fp:
                    tree = ET.parse(fp)
                for cell in tree.getroot().iter():
                    if cell.tag.split("}")[-1] != "c":
                        continue
                    cell_type = cell.attrib.get("t")
                    value_el = next((ch for ch in list(cell) if ch.tag.split("}")[-1] == "v"), None)
                    if value_el is None or value_el.text is None:
                        continue
                    raw_value = value_el.text.strip()
                    if not raw_value:
                        continue
                    if cell_type == "s":
                        try:
                            idx = int(raw_value)
                            if 0 <= idx < len(shared_strings):
                                lines.append(shared_strings[idx])
                        except ValueError:
                            continue
                    else:
                        lines.append(raw_value)

            return "\n".join(lines)
    except Exception:
        return ""


def extract_text_for_file(path: Path, *, max_pages: int | None = None) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path, max_pages=max_pages)
    if suffix == ".docx":
        return _extract_docx_text(path)
    if suffix == ".doc":
        return _extract_doc_text(path)
    if suffix == ".txt":
        return _extract_txt_text(path)
    if suffix == ".xlsx":
        return _extract_xlsx_text(path)
    return ""


def build_normalized_text(
    *,
    documents: Iterable,
    storage_root: str,
    max_chars: int,
    max_files: int | None = None,
    max_pages: int | None = None,
) -> str:
    chunks: list[str] = []

    for idx, doc in enumerate(documents):
        if max_files is not None and idx >= max(1, max_files):
            break
        file_path = Path(storage_root) / doc.storage_path
        if not file_path.exists() or not file_path.is_file():
            continue

        text = extract_text_for_file(file_path, max_pages=max_pages).strip()
        if not text:
            continue

        chunks.append(f"=== {doc.file_name} ===\n{text}\n")

    merged = "\n".join(chunks).strip()
    if len(merged) < 300:
        raise NoExtractableTextError("No extractable text")

    if len(merged) > max_chars:
        merged = merged[:max_chars] + "\n...TRUNCATED"

    return merged


def split_text_into_chunks(
    text: str,
    *,
    max_chunk_chars: int = 12000,
) -> list[str]:
    normalized = (text or "").strip()
    if not normalized:
        return []

    # Prefer splitting by document boundaries produced by build_normalized_text.
    sections = [part.strip() for part in normalized.split("\n=== ") if part.strip()]
    chunks: list[str] = []
    current = ""

    for idx, section in enumerate(sections):
        block = section if idx == 0 and section.startswith("===") else f"=== {section}"
        if len(block) > max_chunk_chars:
            start = 0
            while start < len(block):
                piece = block[start : start + max_chunk_chars]
                chunks.append(piece)
                start += max_chunk_chars
            continue
        if not current:
            current = block
            continue
        candidate = f"{current}\n\n{block}"
        if len(candidate) <= max_chunk_chars:
            current = candidate
        else:
            chunks.append(current)
            current = block

    if current:
        chunks.append(current)

    return chunks
