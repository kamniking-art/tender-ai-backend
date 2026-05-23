#!/usr/bin/env python3
"""Seed baseline policies for a company.

Usage:
    python scripts/seed_policies.py --company-id <UUID>

Idempotent: safe to run multiple times — existing policy_types are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from uuid import UUID

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.core.database import AsyncSessionLocal
from app.policy_engine.seed import BASE_POLICIES, run_seed


async def main() -> None:
    parser = argparse.ArgumentParser(description="Seed baseline policies for a company.")
    parser.add_argument(
        "--company-id",
        required=True,
        metavar="UUID",
        help="UUID of the company to seed policies for",
    )
    args = parser.parse_args()

    try:
        company_id = UUID(args.company_id)
    except ValueError:
        print(f"ERROR: '{args.company_id}' is not a valid UUID", file=sys.stderr)
        sys.exit(1)

    print(f"Seeding {len(BASE_POLICIES)} policies for company {company_id} …")

    async with AsyncSessionLocal() as db:
        result = await run_seed(db, company_id)

    print(json.dumps(result, ensure_ascii=False, indent=2))

    total = len(result["inserted"]) + len(result["skipped_existing"]) + len(result["skipped_invalid"])
    print(
        f"\nDone. inserted={len(result['inserted'])}  "
        f"skipped_existing={len(result['skipped_existing'])}  "
        f"skipped_invalid={len(result['skipped_invalid'])}  "
        f"total={total}"
    )


if __name__ == "__main__":
    asyncio.run(main())
