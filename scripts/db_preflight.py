#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
import asyncio

import psycopg2
from psycopg2 import OperationalError
import asyncpg


def fail(msg: str) -> int:
    print(f"DB preflight FAILED: {msg}")
    return 2


def main() -> int:
    dsn_sync = os.getenv("DATABASE_URL_SYNC", "").strip()
    dsn_async = os.getenv("DATABASE_URL", "").strip()
    if not dsn_sync and dsn_async:
        dsn_sync = dsn_async.replace("postgresql+asyncpg://", "postgresql://")
    if not dsn_async and dsn_sync:
        dsn_async = dsn_sync.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
    if not dsn_sync:
        return fail("DATABASE_URL_SYNC is empty")
    if not dsn_async:
        return fail("DATABASE_URL is empty")
    dsn_sync = dsn_sync.replace("postgresql+psycopg2://", "postgresql://")
    dsn_sync = dsn_sync.replace("postgresql+asyncpg://", "postgresql://")
    dsn_async = dsn_async.replace("postgresql+psycopg2://", "postgresql://")
    dsn_async = dsn_async.replace("postgresql+asyncpg://", "postgresql://")

    attempts = int(os.getenv("DB_PREFLIGHT_ATTEMPTS", "5") or "5")
    sleep_sec = float(os.getenv("DB_PREFLIGHT_SLEEP_SEC", "2") or "2")

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            with psycopg2.connect(dsn_sync) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            print(f"DB preflight sync OK on attempt {attempt}/{attempts}")
            asyncio.run(_async_check(dsn_async))
            print(f"DB preflight async OK on attempt {attempt}/{attempts}")
            print(f"DB preflight OK on attempt {attempt}/{attempts}")
            return 0
        except OperationalError as exc:
            last_error = str(exc).splitlines()[0]
            print(f"DB preflight retry {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(sleep_sec)
        except Exception as exc:  # pragma: no cover
            last_error = f"{exc.__class__.__name__}: {exc}"
            print(f"DB preflight retry {attempt}/{attempts}: {last_error}")
            if attempt < attempts:
                time.sleep(sleep_sec)

    return fail(last_error or "unknown error")


async def _async_check(dsn_async: str) -> None:
    conn = await asyncpg.connect(dsn_async, timeout=5)
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
