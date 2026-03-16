#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time

import psycopg2
from psycopg2 import OperationalError


def fail(msg: str) -> int:
    print(f"DB preflight FAILED: {msg}")
    return 2


def main() -> int:
    dsn = os.getenv("DATABASE_URL_SYNC", "").strip()
    if not dsn:
        return fail("DATABASE_URL_SYNC is empty")
    dsn = dsn.replace("postgresql+psycopg2://", "postgresql://")
    dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    attempts = int(os.getenv("DB_PREFLIGHT_ATTEMPTS", "5") or "5")
    sleep_sec = float(os.getenv("DB_PREFLIGHT_SLEEP_SEC", "2") or "2")

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            with psycopg2.connect(dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
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


if __name__ == "__main__":
    raise SystemExit(main())
