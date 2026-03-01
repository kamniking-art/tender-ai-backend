from __future__ import annotations

import csv
import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator

from app.ingestion.eis_opendata.schemas import OpenDataCandidate

logger = logging.getLogger("uvicorn.error")


def iter_candidates_from_file(file_path: Path, max_records_per_file: int) -> tuple[Iterator[OpenDataCandidate], bool]:
    suffix = file_path.suffix.lower()

    if suffix == ".zip":
        return _iter_from_zip(file_path, max_records_per_file)
    if suffix == ".csv":
        return _iter_from_csv_path(file_path, max_records_per_file), False
    if suffix in {".xml"}:
        return _iter_from_xml_path(file_path, max_records_per_file), False
    if suffix in {".json"}:
        return _iter_from_json_path(file_path, max_records_per_file), False

    logger.warning("eis_opendata skip unsupported format: path=%s", file_path)
    return iter(()), False


def _iter_from_zip(file_path: Path, max_records: int) -> tuple[Iterator[OpenDataCandidate], bool]:
    def _generator() -> Iterator[OpenDataCandidate]:
        emitted = 0
        truncated = False

        with zipfile.ZipFile(file_path) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            for name in names:
                lower = name.lower()
                if lower.endswith(".csv"):
                    with zf.open(name, "r") as fh:
                        for cand in _iter_from_csv_stream(fh, max_records - emitted):
                            yield cand
                            emitted += 1
                            if emitted >= max_records:
                                truncated = True
                                break
                elif lower.endswith(".xml"):
                    with zf.open(name, "r") as fh:
                        for cand in _iter_from_xml_stream(fh, max_records - emitted):
                            yield cand
                            emitted += 1
                            if emitted >= max_records:
                                truncated = True
                                break
                elif lower.endswith(".json"):
                    with zf.open(name, "r") as fh:
                        for cand in _iter_from_json_stream(fh, max_records - emitted):
                            yield cand
                            emitted += 1
                            if emitted >= max_records:
                                truncated = True
                                break
                else:
                    logger.warning("eis_opendata skip unsupported zip member: %s", name)

                if emitted >= max_records:
                    break

        if truncated:
            logger.warning("eis_opendata parser truncated: file=%s max_records=%s", file_path, max_records)

    # pre-check truncation info is emitted by logger, return False here since generator handles it.
    return _generator(), False


def _iter_from_csv_path(file_path: Path, limit: int) -> Iterator[OpenDataCandidate]:
    with file_path.open("rb") as fh:
        yield from _iter_from_csv_stream(fh, limit)


def _iter_from_csv_stream(binary_stream: io.BufferedReader | zipfile.ZipExtFile, limit: int) -> Iterator[OpenDataCandidate]:
    text_stream = io.TextIOWrapper(binary_stream, encoding="utf-8-sig", errors="ignore", newline="")
    sample = text_stream.read(4096)
    text_stream.seek(0)

    delimiter = ";"
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t,")
        delimiter = dialect.delimiter
    except csv.Error:
        pass

    reader = csv.DictReader(text_stream, delimiter=delimiter)
    count = 0
    for row in reader:
        if limit <= 0 or count >= limit:
            break
        candidate = _normalize_record(row)
        if candidate is not None:
            yield candidate
            count += 1


def _iter_from_xml_path(file_path: Path, limit: int) -> Iterator[OpenDataCandidate]:
    with file_path.open("rb") as fh:
        yield from _iter_from_xml_stream(fh, limit)


def _iter_from_xml_stream(binary_stream: io.BufferedReader | zipfile.ZipExtFile, limit: int) -> Iterator[OpenDataCandidate]:
    count = 0
    try:
        context = ET.iterparse(binary_stream, events=("end",))
        for _, elem in context:
            if limit <= 0 or count >= limit:
                break
            children = list(elem)
            if not children:
                continue

            row: dict[str, str] = {}
            for child in children:
                text = (child.text or "").strip()
                if not text:
                    continue
                row[_strip_ns(child.tag)] = text

            if not row:
                continue

            candidate = _normalize_record(row)
            if candidate is not None:
                yield candidate
                count += 1

            elem.clear()
    except ET.ParseError:
        logger.warning("eis_opendata invalid XML in stream", exc_info=True)


def _iter_from_json_path(file_path: Path, limit: int) -> Iterator[OpenDataCandidate]:
    with file_path.open("rb") as fh:
        yield from _iter_from_json_stream(fh, limit)


def _iter_from_json_stream(binary_stream: io.BufferedReader | zipfile.ZipExtFile, limit: int) -> Iterator[OpenDataCandidate]:
    try:
        payload = json.load(binary_stream)
    except json.JSONDecodeError:
        logger.warning("eis_opendata invalid JSON payload", exc_info=True)
        return

    rows: Iterable[dict]
    if isinstance(payload, list):
        rows = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        # common containers: data/items/results
        for key in ("data", "items", "results"):
            section = payload.get(key)
            if isinstance(section, list):
                rows = [x for x in section if isinstance(x, dict)]
                break
        else:
            rows = [payload]
    else:
        rows = []

    count = 0
    for row in rows:
        if limit <= 0 or count >= limit:
            break
        candidate = _normalize_record(row)
        if candidate is not None:
            yield candidate
            count += 1


def _normalize_record(row: dict[str, object]) -> OpenDataCandidate | None:
    normalized = {_norm_key(k): v for k, v in row.items()}

    external_id = _first_str(
        normalized,
        "externalid",
        "external_id",
        "id",
        "idizvesheniya",
        "idizv",
        "noticeid",
        "noticenumber",
        "notificationnumber",
        "purchasenumber",
        "regnum",
        "number",
    )
    if not external_id:
        return None

    title = _first_str(
        normalized,
        "title",
        "name",
        "purchaseobjectinfo",
        "purchase_name",
        "purchaseobject",
        "object",
    )
    customer_name = _first_str(normalized, "customername", "customer", "fullname", "organization")
    region = _first_str(normalized, "region", "regionname", "subject", "deliveryplace")

    law_raw = _first_str(normalized, "law", "fz", "lawtype", "procurementtype")
    procurement_type = _normalize_law(law_raw)

    nmck = _parse_decimal(_first_str(normalized, "nmck", "maxprice", "price", "initialsum", "sum", "amount"))
    published_at = _parse_datetime(
        _first_str(normalized, "publisheddate", "publishdate", "createdate", "publicationdate")
    )
    submission_deadline = _parse_datetime(
        _first_str(
            normalized,
            "submissiondeadline",
            "applicationdeadline",
            "enddate",
            "deadline",
            "submissionclosedate",
        )
    )

    return OpenDataCandidate(
        external_id=external_id,
        title=title,
        customer_name=customer_name,
        region=region,
        procurement_type=procurement_type,
        nmck=nmck,
        published_at=published_at,
        submission_deadline=submission_deadline,
    )


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", "", key.lower())


def _first_str(mapping: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = mapping.get(_norm_key(key))
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _normalize_law(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.lower()
    if "44" in text:
        return "44fz"
    if "223" in text:
        return "223fz"
    return None


def _parse_decimal(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d,.-]", "", raw).replace(",", ".")
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None

    text = raw.strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC)
        except ValueError:
            continue

    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]
