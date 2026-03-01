from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Company
from app.tender_alerts.schemas import AlertCategory, AlertTenderItem
from app.tender_alerts.service import build_alert_digest
from app.tender_analysis.model import TenderAnalysis
from app.tenders.model import Tender


class TelegramConfigError(Exception):
    def __init__(self, missing_fields: list[str]) -> None:
        super().__init__("Telegram config is incomplete")
        self.missing_fields = missing_fields


@dataclass
class TelegramConfig:
    enabled: bool
    bot_token: str
    chat_id: str
    send_from: str | None
    send_to: str | None
    min_interval_minutes: int


@dataclass
class NotificationStats:
    sent_messages: int = 0
    sent_items: int = 0


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return time(hour=hour, minute=minute)


def within_send_window(send_from: str | None, send_to: str | None, now_local: datetime | None = None) -> bool:
    if send_from is None and send_to is None:
        return True

    start = _parse_hhmm(send_from)
    end = _parse_hhmm(send_to)
    if start is None or end is None:
        return True

    current = (now_local or datetime.now()).time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def is_min_interval_elapsed(last_sent_at: str | None, min_interval_minutes: int, now_utc: datetime | None = None) -> bool:
    if min_interval_minutes <= 0:
        return True
    if not last_sent_at:
        return True
    parsed = _parse_iso(last_sent_at)
    if parsed is None:
        return True
    now = now_utc or _now_utc()
    return (now - parsed) >= timedelta(minutes=min_interval_minutes)


def _extract_telegram_config(profile: dict) -> TelegramConfig | None:
    raw = profile.get("telegram") if isinstance(profile.get("telegram"), dict) else None
    if raw is None:
        return None

    enabled = bool(raw.get("enabled", False))
    send_window = raw.get("send_window") if isinstance(raw.get("send_window"), dict) else {}
    min_interval = raw.get("min_interval_minutes", 30)
    try:
        min_interval_value = max(0, int(min_interval))
    except (TypeError, ValueError):
        min_interval_value = 30

    return TelegramConfig(
        enabled=enabled,
        bot_token=str(raw.get("bot_token") or ""),
        chat_id=str(raw.get("chat_id") or ""),
        send_from=send_window.get("from") if isinstance(send_window.get("from"), str) else None,
        send_to=send_window.get("to") if isinstance(send_window.get("to"), str) else None,
        min_interval_minutes=min_interval_value,
    )


def validate_telegram_config(profile: dict) -> TelegramConfig:
    cfg = _extract_telegram_config(profile)
    missing_fields: list[str] = []

    if cfg is None:
        missing_fields.extend(["telegram.enabled", "telegram.bot_token", "telegram.chat_id"])
    else:
        if not cfg.enabled:
            missing_fields.append("telegram.enabled")
        if not cfg.bot_token:
            missing_fields.append("telegram.bot_token")
        if not cfg.chat_id:
            missing_fields.append("telegram.chat_id")

    if missing_fields:
        raise TelegramConfigError(missing_fields)

    return cfg  # type: ignore[return-value]


def _ensure_state(profile: dict) -> dict:
    state = profile.get("telegram_state") if isinstance(profile.get("telegram_state"), dict) else {}
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}

    for cat in ("new", "deadline_24h", "risky"):
        bucket = sent.get(cat)
        if not isinstance(bucket, dict):
            sent[cat] = {}

    state.setdefault("last_sent_at", None)
    state["sent"] = sent
    profile["telegram_state"] = state
    return state


def _prune_bucket(bucket: dict[str, str], limit: int = 500) -> dict[str, str]:
    items = sorted(bucket.items(), key=lambda kv: _parse_iso(kv[1]) or datetime.min.replace(tzinfo=UTC))
    if len(items) <= limit:
        return dict(items)
    return dict(items[-limit:])


def dedup_unsent_items(category: str, items: list[AlertTenderItem], state: dict) -> list[AlertTenderItem]:
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
    bucket = sent.get(category) if isinstance(sent.get(category), dict) else {}
    unsent: list[AlertTenderItem] = []
    for item in items:
        if str(item.tender_id) not in bucket:
            unsent.append(item)
    return unsent


def mark_items_sent(category: str, items: list[AlertTenderItem], state: dict, sent_at: datetime) -> None:
    sent = state.setdefault("sent", {})
    bucket = sent.setdefault(category, {})
    for item in items:
        bucket[str(item.tender_id)] = _iso(sent_at)
    sent[category] = _prune_bucket(bucket, limit=500)
    state["last_sent_at"] = _iso(sent_at)


async def _get_alert_items_for_company(db: AsyncSession, company_id: UUID, category: AlertCategory) -> list[AlertTenderItem]:
    digest = await build_alert_digest(
        db,
        company_id=company_id,
        user_id=uuid4(),
        since=None,
        include_acknowledged=True,
        categories=[category],
        limit=100,
    )
    return digest.items


async def _get_nmck_map(db: AsyncSession, company_id: UUID, tender_ids: list[UUID]) -> dict[UUID, object]:
    if not tender_ids:
        return {}
    rows = (
        await db.execute(
            select(Tender.id, Tender.nmck)
            .where(Tender.company_id == company_id, Tender.id.in_(tender_ids))
        )
    ).all()
    return {row.id: row.nmck for row in rows}


async def _get_risk_flags_map(db: AsyncSession, company_id: UUID, tender_ids: list[UUID]) -> dict[UUID, list[str]]:
    if not tender_ids:
        return {}
    rows = (
        await db.execute(
            select(TenderAnalysis.tender_id, TenderAnalysis.risk_flags)
            .where(TenderAnalysis.company_id == company_id, TenderAnalysis.tender_id.in_(tender_ids))
        )
    ).all()

    result: dict[UUID, list[str]] = {}
    for row in rows:
        flags: list[str] = []
        if isinstance(row.risk_flags, list):
            for item in row.risk_flags:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("code")
                    if title:
                        flags.append(str(title))
                elif isinstance(item, str):
                    flags.append(item)
        result[row.tender_id] = flags
    return result


def _build_link(tender_id: UUID) -> str:
    base = settings.public_base_url.rstrip("/")
    return f"{base}/web/tenders/{tender_id}"


def _fmt_deadline(value: datetime | None) -> str:
    return value.isoformat().replace("+00:00", "Z") if value else "—"


def _fmt_nmck(value: object) -> str:
    if value is None:
        return "—"
    return str(value)


def _build_new_message(items: list[AlertTenderItem], nmck_map: dict[UUID, object]) -> str:
    lines = [f"Новые тендеры ({len(items)}):"]
    for idx, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{idx}) {item.title or 'Без названия'}",
                f"Дедлайн: {_fmt_deadline(item.deadline_at)}",
                f"НМЦК: {_fmt_nmck(nmck_map.get(item.tender_id))}",
                f"Риск: {item.risk_score if item.risk_score is not None else '—'}",
                _build_link(item.tender_id),
            ]
        )
    return "\n".join(lines)


def _build_deadline_message(items: list[AlertTenderItem]) -> str:
    lines = [f"Дедлайн <24ч ({len(items)}):"]
    for idx, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{idx}) {item.title or 'Без названия'}",
                f"Дедлайн: {_fmt_deadline(item.deadline_at)}",
                _build_link(item.tender_id),
            ]
        )
    return "\n".join(lines)


def _build_risky_message(items: list[AlertTenderItem], flags_map: dict[UUID, list[str]]) -> str:
    lines = [f"Высокий риск ({len(items)}):"]
    for idx, item in enumerate(items, start=1):
        flags = ", ".join(flags_map.get(item.tender_id, [])[:3]) or "—"
        lines.extend(
            [
                f"{idx}) {item.title or 'Без названия'}",
                f"Риск: {item.risk_score if item.risk_score is not None else '—'}",
                f"Флаги: {flags}",
                f"Рекомендация: {item.recommendation or '—'}",
                _build_link(item.tender_id),
            ]
        )
    return "\n".join(lines)


async def process_company_notifications(db: AsyncSession, company: Company, client) -> NotificationStats:
    profile = copy.deepcopy(company.profile or {})
    cfg = _extract_telegram_config(profile)
    if cfg is None or not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
        return NotificationStats()

    if not within_send_window(cfg.send_from, cfg.send_to):
        return NotificationStats()

    state = _ensure_state(profile)
    if not is_min_interval_elapsed(state.get("last_sent_at"), cfg.min_interval_minutes):
        return NotificationStats()

    now = _now_utc()

    new_items = await _get_alert_items_for_company(db, company.id, AlertCategory.NEW)
    risky_items = await _get_alert_items_for_company(db, company.id, AlertCategory.RISKY)
    deadline_items = await _get_alert_items_for_company(db, company.id, AlertCategory.DEADLINE_SOON)
    deadline_24h = [
        item
        for item in deadline_items
        if item.deadline_at is not None and item.deadline_at <= (now + timedelta(hours=24))
    ]

    pending = {
        "new": dedup_unsent_items("new", new_items, state)[:5],
        "deadline_24h": dedup_unsent_items("deadline_24h", deadline_24h, state)[:5],
        "risky": dedup_unsent_items("risky", risky_items, state)[:5],
    }

    stats = NotificationStats()

    all_ids: list[UUID] = []
    for bucket in pending.values():
        all_ids.extend([item.tender_id for item in bucket])

    nmck_map = await _get_nmck_map(db, company.id, all_ids)
    flags_map = await _get_risk_flags_map(db, company.id, all_ids)

    category_order = ["new", "deadline_24h", "risky"]
    for category in category_order:
        items = pending[category]
        if not items:
            continue

        if category == "new":
            text = _build_new_message(items, nmck_map)
        elif category == "deadline_24h":
            text = _build_deadline_message(items)
        else:
            text = _build_risky_message(items, flags_map)

        await client.send_message(bot_token=cfg.bot_token, chat_id=cfg.chat_id, text=text)
        mark_items_sent(category, items, state, sent_at=now)
        stats.sent_messages += 1
        stats.sent_items += len(items)

    if stats.sent_messages > 0:
        company.profile = profile
        await db.commit()

    return stats
