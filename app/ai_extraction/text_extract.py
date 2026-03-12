from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable
from zipfile import ZipFile

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


def extract_text_for_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text(path)
    if suffix == ".docx":
        return _extract_docx_text(path)
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
