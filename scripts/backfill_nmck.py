#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select

from app.core.database import AsyncSessionLocal
from app.models import User
from app.tender_documents.service import fetch_nmck_from_source_page
from app.tenders.model import Tender

MAX_VALID_NMCK = Decimal("1000000000000")


@dataclass
class SampleRow:
    external_id: str | None
    tender_id: str
    old_value: str | None
    new_value: str | None
    action: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "tender_id": self.tender_id,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "source": self.action,
            "reason": self.reason,
        }


def _is_id_like(value: Decimal) -> bool:
    if value != value.to_integral_value():
        return False
    digits = "".join(ch for ch in str(value.to_integral_value()) if ch.isdigit())
    return len(digits) >= 13


def is_invalid_nmck(value: Decimal | None) -> bool:
    if value is None:
        return False
    if value <= 0 or value > MAX_VALID_NMCK:
        return True
    return _is_id_like(value)


def _nmck_sort_key(tender: Tender) -> tuple[int, float]:
    if tender.published_at is None:
        return (1, 0.0)
    return (0, tender.published_at.timestamp())


async def _load_demo_company_id() -> UUID | None:
    async with AsyncSessionLocal() as db:
        user = await db.scalar(select(User).where(User.email == "admin@demo.ru"))
        if user is None:
            return None
        return user.company_id


def _latest_200_statement(company_id: UUID) -> Select[tuple[Tender]]:
    return (
        select(Tender)
        .where(Tender.company_id == company_id)
        .order_by(Tender.published_at.desc().nullslast(), Tender.created_at.desc().nullslast())
        .limit(200)
    )


def _ui_dash_count(rows: list[Tender]) -> int:
    return sum(1 for row in rows if row.nmck is None or is_invalid_nmck(row.nmck))


def _ui_valid_count(rows: list[Tender]) -> int:
    return sum(1 for row in rows if row.nmck is not None and not is_invalid_nmck(row.nmck))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill/cleanup invalid NMCK values in tenders table.")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without committing.")
    parser.add_argument("--sample-limit", type=int, default=10, help="How many sample rows to include in report.")
    args = parser.parse_args()

    report: dict[str, Any] = {
        "found_invalid_total": 0,
        "group_a_with_source_url": 0,
        "group_b_without_source_url": 0,
        "reparsed_success": 0,
        "nulled_after_failed_reparse": 0,
        "nulled_no_source_url": 0,
        "samples": [],
    }
    samples: list[SampleRow] = []

    demo_company_id = await _load_demo_company_id()

    async with AsyncSessionLocal() as db:
        before_last200: dict[str, int] | None = None
        if demo_company_id is not None:
            before_rows = list((await db.scalars(_latest_200_statement(demo_company_id))).all())
            before_last200 = {
                "total": len(before_rows),
                "valid_nmck": _ui_valid_count(before_rows),
                "ui_dash_or_unset": _ui_dash_count(before_rows),
            }

        rows = list((await db.scalars(select(Tender).where(Tender.nmck.is_not(None)))).all())
        invalid_rows = [row for row in rows if is_invalid_nmck(row.nmck)]
        invalid_rows.sort(key=_nmck_sort_key, reverse=True)
        report["found_invalid_total"] = len(invalid_rows)

        fetch_cache: dict[str, Decimal | None] = {}

        for row in invalid_rows:
            old_value = str(row.nmck) if row.nmck is not None else None
            if row.source_url:
                report["group_a_with_source_url"] += 1
                if row.source_url not in fetch_cache:
                    result = await fetch_nmck_from_source_page(row.source_url)
                    fetched = result.nmck
                    if fetched is not None and is_invalid_nmck(fetched):
                        fetched = None
                    fetch_cache[row.source_url] = fetched
                fetched_nmck = fetch_cache[row.source_url]
                if fetched_nmck is not None:
                    row.nmck = fetched_nmck
                    report["reparsed_success"] += 1
                    if len(samples) < args.sample_limit:
                        samples.append(
                            SampleRow(
                                external_id=row.external_id,
                                tender_id=str(row.id),
                                old_value=old_value,
                                new_value=str(fetched_nmck),
                                action="reparsed",
                                reason="source_url",
                            )
                        )
                else:
                    row.nmck = None
                    report["nulled_after_failed_reparse"] += 1
                    if len(samples) < args.sample_limit:
                        samples.append(
                            SampleRow(
                                external_id=row.external_id,
                                tender_id=str(row.id),
                                old_value=old_value,
                                new_value=None,
                                action="nulled",
                                reason="reparse_failed",
                            )
                        )
            else:
                report["group_b_without_source_url"] += 1
                row.nmck = None
                report["nulled_no_source_url"] += 1
                if len(samples) < args.sample_limit:
                    samples.append(
                        SampleRow(
                            external_id=row.external_id,
                            tender_id=str(row.id),
                            old_value=old_value,
                            new_value=None,
                            action="nulled",
                            reason="no_source_url",
                        )
                    )

        if args.dry_run:
            await db.rollback()
        else:
            await db.commit()

        after_last200: dict[str, int] | None = None
        if demo_company_id is not None:
            after_rows = list((await db.scalars(_latest_200_statement(demo_company_id))).all())
            after_last200 = {
                "total": len(after_rows),
                "valid_nmck": _ui_valid_count(after_rows),
                "ui_dash_or_unset": _ui_dash_count(after_rows),
            }

    report["samples"] = [item.as_dict() for item in samples]
    report["latest_200_demo_company"] = {
        "before": before_last200,
        "after": after_last200,
    }
    report["dry_run"] = bool(args.dry_run)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
