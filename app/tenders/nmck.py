from __future__ import annotations

from decimal import Decimal, InvalidOperation

MAX_SANE_NMCK = Decimal("1000000000000")


def get_sane_nmck(value: object | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if parsed <= 0 or parsed > MAX_SANE_NMCK:
        return None
    return parsed
