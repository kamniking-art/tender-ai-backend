from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from zipfile import ZipFile, BadZipFile

from docx import Document as DocxDocument
from pypdf import PdfReader

logger = logging.getLogger(__name__)

# ── NMCK keyword list (case-insensitive search) ────────────────────────────────

_NMCK_KEYWORDS = [
    "начальная (максимальная) цена",
    "начальная максимальная цена",
    "цена договора составляет",
    "цена контракта составляет",
    "максимальная цена договора",
    "нмцк",
    "нмцд",
    "цена договора",
    "цена контракта",
    "начальная цена",
]

# Compiled once: thousands-separated "444 800" OR plain "44800"
_NUMBER_RE = re.compile(r"\d{1,3}(?:[ \xa0]\d{3})+|\d+")


class NoExtractableTextError(ValueError):
    pass


# ── XLSX helpers ───────────────────────────────────────────────────────────────


def _col_to_num(col_str: str) -> int:
    """Convert column letter(s) 'A'→1, 'Z'→26, 'AA'→27, …"""
    num = 0
    for ch in col_str.upper():
        num = num * 26 + (ord(ch) - ord("A") + 1)
    return num


def _parse_cell_ref(ref: str) -> tuple[int, int]:
    """Parse 'A1', 'BC42' → (row, col) 1-based. Returns (0, 0) on failure."""
    m = re.match(r"([A-Za-z]+)(\d+)", ref or "")
    if not m:
        return 0, 0
    return int(m.group(2)), _col_to_num(m.group(1))


def _load_shared_strings(zf: ZipFile, names: set[str]) -> list[str]:
    shared: list[str] = []
    if "xl/sharedStrings.xml" not in names:
        return shared
    with zf.open("xl/sharedStrings.xml") as fp:
        tree = ET.parse(fp)
    for el in tree.getroot().iter():
        if el.tag.split("}")[-1] == "t" and el.text:
            shared.append(el.text.strip())
    return shared


def _cell_value(
    cell_el: ET.Element, shared_strings: list[str]
) -> tuple[str | None, Decimal | None]:
    """
    Return (text, numeric) for a cell element.
    - Shared-string cells → (text, None)
    - Pure-numeric cells  → (None, Decimal)
    - Unrecognised        → (None, None)
    """
    cell_type = cell_el.attrib.get("t")
    value_el = next(
        (ch for ch in cell_el if ch.tag.split("}")[-1] == "v"), None
    )
    if value_el is None or not value_el.text:
        return None, None

    raw = value_el.text.strip()
    if not raw:
        return None, None

    if cell_type == "s":
        try:
            idx = int(raw)
            text = shared_strings[idx] if 0 <= idx < len(shared_strings) else None
        except ValueError:
            text = None
        return text, None

    # Numeric or formula result
    try:
        return None, Decimal(raw.replace(",", "."))
    except InvalidOperation:
        # Treat as plain text (inline string, bool literal, etc.)
        return raw if raw else None, None


def _parse_sheet_rows(
    zf: ZipFile, sheet_name: str, shared_strings: list[str]
) -> dict[int, list[tuple[int, str | None, Decimal | None]]]:
    """
    Parse one worksheet into a dict:
        row_number → [(col_number, text_or_None, numeric_or_None), …]
    Cells are sorted by column within each row.
    """
    rows: dict[int, list[tuple[int, str | None, Decimal | None]]] = {}
    with zf.open(sheet_name) as fp:
        tree = ET.parse(fp)

    for row_el in tree.getroot().iter():
        if row_el.tag.split("}")[-1] != "row":
            continue
        raw_r = row_el.attrib.get("r")
        if not raw_r:
            continue
        try:
            row_num = int(raw_r)
        except ValueError:
            continue

        cells: list[tuple[int, str | None, Decimal | None]] = []
        for cell in row_el:
            if cell.tag.split("}")[-1] != "c":
                continue
            _, col = _parse_cell_ref(cell.attrib.get("r", ""))
            text, numeric = _cell_value(cell, shared_strings)
            if text is not None or numeric is not None:
                cells.append((col, text, numeric))

        if cells:
            cells.sort(key=lambda x: x[0])
            rows[row_num] = cells

    return rows


# ── Text extractors ────────────────────────────────────────────────────────────


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
    """
    Structured extraction: preserves row/column context.
    Each row is emitted as pipe-separated cell values.
    """
    try:
        with ZipFile(path) as zf:
            names = set(zf.namelist())
            shared_strings = _load_shared_strings(zf, names)

            lines: list[str] = []
            sheet_names = sorted(
                n for n in names
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            for sheet_name in sheet_names:
                rows = _parse_sheet_rows(zf, sheet_name, shared_strings)
                for row_num in sorted(rows):
                    parts: list[str] = []
                    for _col, text, numeric in rows[row_num]:
                        if text:
                            parts.append(text)
                        elif numeric is not None:
                            parts.append(str(numeric))
                    if parts:
                        lines.append(" | ".join(parts))

            return "\n".join(lines)
    except Exception:
        return ""


def _extract_zip_text(path: Path) -> str:
    chunks: list[str] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with ZipFile(path) as zf:
                for name in zf.namelist():
                    inner_suffix = Path(name).suffix.lower()
                    if inner_suffix not in {".pdf", ".docx", ".doc", ".txt", ".xlsx"}:
                        continue
                    extracted = tmp_path / Path(name).name
                    extracted.write_bytes(zf.read(name))
                    text = extract_text_for_file(extracted).strip()
                    if text:
                        chunks.append(f"=== {Path(name).name} ===\n{text}")
    except BadZipFile:
        logger.warning("Not a valid zip file: %s", path)
    except Exception as exc:
        logger.warning("zip extraction error for %s: %s", path, exc)
    return "\n".join(chunks)


# ── NMCK semantic extraction ───────────────────────────────────────────────────


def _extract_inline_nmck(text: str) -> Decimal | None:
    """
    Extract NMCK when the number is embedded inside the label cell text, e.g.:
    "Начальная (максимальная) цена Договора составляет: 444 800 рублей 00 копеек"
    → Decimal('444800')

    Strategy:
    - Find the rightmost keyword position in the text.
    - In the tail after the keyword, collect all digit sequences.
    - Thousands-separated "444 800" is joined; kopek "00" is filtered by > 1000.
    """
    text_lower = text.lower()
    kw_end = -1
    for kw in _NMCK_KEYWORDS:
        idx = text_lower.find(kw)
        if idx >= 0:
            pos = idx + len(kw)
            if pos > kw_end:
                kw_end = pos
    if kw_end < 0:
        return None

    tail = text[kw_end:]
    candidates: list[Decimal] = []
    for m in _NUMBER_RE.findall(tail):
        clean = re.sub(r"[ \xa0]", "", m)
        try:
            val = Decimal(clean)
            if val > 1000:
                candidates.append(val)
        except InvalidOperation:
            pass
    return max(candidates) if candidates else None


def extract_nmck_from_xlsx(path: Path) -> Decimal | None:
    """
    Deterministic NMCK search with row/column context.

    Pass 1 — label-based (returns on first success):
      1a. Inline: number embedded inside the label cell text itself.
      1b. Sibling: numeric cell in the same row as the label.
      1c. Below:   numeric cell in the row immediately below the label.

    Pass 2 — fallback (no label found):
      Take max of all numerics > 1000 in the sheet, but only when the sheet
      has < 20 unique numeric values — prevents grabbing random data from
      large price-list tables.
    """
    candidates: list[Decimal] = []
    all_sheet_nums: list[Decimal] = []

    try:
        with ZipFile(path) as zf:
            names = set(zf.namelist())
            shared_strings = _load_shared_strings(zf, names)

            sheet_names = sorted(
                n for n in names
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")
            )
            for sheet_name in sheet_names:
                rows = _parse_sheet_rows(zf, sheet_name, shared_strings)
                row_nums = sorted(rows)

                for i, row_num in enumerate(row_nums):
                    cells = rows[row_num]

                    # Accumulate all numerics for fallback
                    for _col, _text, numeric in cells:
                        if numeric is not None and numeric > 1000:
                            all_sheet_nums.append(numeric)

                    # Find label cell in this row
                    label_text: str | None = None
                    for _col, text, _numeric in cells:
                        if text and any(kw in text.lower() for kw in _NMCK_KEYWORDS):
                            label_text = text
                            break

                    if label_text is None:
                        continue

                    # 1a — number embedded inside the label text
                    inline = _extract_inline_nmck(label_text)
                    if inline is not None:
                        candidates.append(inline)

                    # 1b — numeric sibling cells in the same row
                    for _col, _text, numeric in cells:
                        if numeric is not None and numeric > 1000:
                            candidates.append(numeric)

                    # 1c — row below (value sometimes sits under label)
                    if i + 1 < len(row_nums):
                        for _col, _text, numeric in rows[row_nums[i + 1]]:
                            if numeric is not None and numeric > 1000:
                                candidates.append(numeric)

    except Exception as exc:
        logger.warning("nmck xlsx extraction error for %s: %s", path, exc)

    # Pass 1: label-based winner
    if candidates:
        return max(candidates)

    # Pass 2: fallback — only for "small" sheets (< 20 unique values)
    unique_nums = set(all_sheet_nums)
    if unique_nums and len(unique_nums) < 20:
        return max(unique_nums)

    return None


# ── Public API ─────────────────────────────────────────────────────────────────


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
    if suffix == ".zip":
        return _extract_zip_text(path)
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

        # For xlsx files try deterministic NMCK extraction and prepend it
        prefix = ""
        if file_path.suffix.lower() == ".xlsx":
            try:
                nmck = extract_nmck_from_xlsx(file_path)
                if nmck is not None:
                    prefix = f"НМЦК: {nmck} руб.\n"
            except Exception:
                pass

        chunks.append(f"=== {doc.file_name} ===\n{prefix}{text}\n")

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
