"""OperationalAlertsScheduler — periodic operational health checks.

Runs every N minutes (default 30) and fires Telegram alerts on:
  1. overdue_tasks > 0          (v_queue_backlog)
  2. success_rate_24h < 50%     (v_health_per_tenant)
  3. provider error spike       (v_provider_errors, error_count_24h > threshold)
  4. telegram stale             (company.profile: enabled but last_sent_at > 24h)

Follows the same asyncio-loop pattern as other schedulers in this project.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal

logger = logging.getLogger("uvicorn.error")

_DEFAULT_INTERVAL_MINUTES = 30
_MIN_CALLS_FOR_RATE_ALERT = 5          # ignore companies with < 5 calls/24h
_PROVIDER_ERROR_THRESHOLD = 10         # alert when provider errors/24h exceeds this
_TELEGRAM_STALE_HOURS = 24             # alert if last_sent_at older than this


class OperationalAlertsScheduler:
    """Periodically checks operational metrics and fires Telegram alerts."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        interval = max(60, settings.operational_alerts_interval_minutes * 60)
        while self._running:
            try:
                await self._run_once()
            except Exception:
                logger.exception("OperationalAlertsScheduler iteration failed")
            await asyncio.sleep(interval)

    async def _run_once(self) -> None:
        async with AsyncSessionLocal() as db:
            await _check_queue_backlog(db)
            await _check_health_per_tenant(db)
            await _check_provider_errors(db)
            await _check_telegram_stale(db)


# ── helpers ──────────────────────────────────────────────────────────────────


async def _notify_company(db: AsyncSession, company_id: object, message: str) -> None:
    """Send a Telegram message to a company if Telegram is configured and enabled."""
    if not company_id:
        return
    try:
        from sqlalchemy import select as _select
        from app.models import Company
        from app.telegram_notify.client import TelegramClient, TelegramSendError
        from app.telegram_notify.service import _extract_telegram_config

        company = await db.scalar(_select(Company).where(Company.id == company_id))
        if company is None or not isinstance(company.profile, dict):
            return

        cfg = _extract_telegram_config(company.profile)
        if not cfg or not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
            return

        client = TelegramClient(timeout_sec=settings.warsaw_timeout_sec)
        try:
            await client.send_message(bot_token=cfg.bot_token, chat_id=cfg.chat_id, text=message)
            logger.info("operational_alert sent: company_id=%s", company_id)
        except TelegramSendError as exc:
            logger.warning("operational_alert telegram failed: company_id=%s reason=%s", company_id, str(exc))
        finally:
            await client.close()

    except Exception:
        logger.exception("operational_alert notify failed: company_id=%s", company_id)


def _query_view(db: AsyncSession, view_name: str):
    return db.execute(text(f"SELECT * FROM {view_name}"))  # noqa: S608


# ── check 1: overdue tasks ────────────────────────────────────────────────────


async def _check_queue_backlog(db: AsyncSession) -> None:
    """Alert per company when overdue_tasks > 0."""
    try:
        result = await _query_view(db, "v_queue_backlog")
        keys = list(result.keys())
        rows = [dict(zip(keys, row)) for row in result.fetchall()]
    except Exception:
        logger.warning("OperationalAlertsScheduler: v_queue_backlog unavailable", exc_info=True)
        return

    for row in rows:
        overdue = row.get("overdue_tasks") or 0
        if overdue <= 0:
            continue
        pending = row.get("pending_tasks") or 0
        company_name = row.get("company_name") or "—"
        logger.warning(
            "operational_alert: overdue_tasks company=%s overdue=%d pending=%d",
            company_name, overdue, pending,
        )
        await _notify_company(
            db,
            row.get("company_id"),
            f"⚠️ Просроченные задачи\n\nПросроченных: {overdue}\nВ очереди: {pending}\n\nПроверьте панель мониторинга.",
        )


# ── check 2: low success rate ─────────────────────────────────────────────────


async def _check_health_per_tenant(db: AsyncSession) -> None:
    """Alert per company when success_rate_24h_pct < 50% with enough calls."""
    try:
        result = await _query_view(db, "v_health_per_tenant")
        keys = list(result.keys())
        rows = [dict(zip(keys, row)) for row in result.fetchall()]
    except Exception:
        logger.warning("OperationalAlertsScheduler: v_health_per_tenant unavailable", exc_info=True)
        return

    for row in rows:
        calls = row.get("calls_24h") or 0
        rate = row.get("success_rate_24h_pct")
        if calls < _MIN_CALLS_FOR_RATE_ALERT or rate is None:
            continue
        try:
            rate_f = float(rate)
        except (TypeError, ValueError):
            continue
        if rate_f >= 50.0:
            continue
        company_name = row.get("company_name") or "—"
        logger.warning(
            "operational_alert: low_success_rate company=%s rate=%.1f%% calls=%d",
            company_name, rate_f, calls,
        )
        await _notify_company(
            db,
            row.get("company_id"),
            f"⚠️ Низкий процент успешных AI-вызовов\n\nУспешных за 24ч: {rate_f:.0f}%\nВсего вызовов: {calls}\n\nПроверьте логи AI-экстракции.",
        )


# ── check 3: provider error spike ────────────────────────────────────────────


async def _check_provider_errors(db: AsyncSession) -> None:
    """Log warning (global) when any provider exceeds error threshold."""
    try:
        result = await _query_view(db, "v_provider_errors")
        keys = list(result.keys())
        rows = [dict(zip(keys, row)) for row in result.fetchall()]
    except Exception:
        logger.warning("OperationalAlertsScheduler: v_provider_errors unavailable", exc_info=True)
        return

    for row in rows:
        error_count = row.get("error_count_24h") or row.get("error_count") or 0
        try:
            error_count = int(error_count)
        except (TypeError, ValueError):
            continue
        if error_count <= _PROVIDER_ERROR_THRESHOLD:
            continue
        provider = row.get("provider") or "unknown"
        last_error = row.get("last_error") or "—"
        logger.warning(
            "operational_alert: provider_errors provider=%s errors_24h=%d last_error=%s",
            provider, error_count, last_error,
        )
        # Provider errors are global — log only, no per-company Telegram notification.


# ── check 4: telegram stale ───────────────────────────────────────────────────


async def _check_telegram_stale(db: AsyncSession) -> None:
    """Log warning when company has Telegram enabled but last_sent_at is stale."""
    try:
        from sqlalchemy import select as _select
        from app.models import Company
        from app.telegram_notify.service import _extract_telegram_config

        companies = list((await db.scalars(_select(Company))).all())
        cutoff = datetime.now(UTC) - timedelta(hours=_TELEGRAM_STALE_HOURS)

        for company in companies:
            if not isinstance(company.profile, dict):
                continue
            cfg = _extract_telegram_config(company.profile)
            if not cfg or not cfg.enabled:
                continue

            state = company.profile.get("telegram_state") or {}
            last_sent_raw = state.get("last_sent_at")
            if last_sent_raw is None:
                continue  # never sent — not an error, may be new company

            # Parse ISO timestamp
            try:
                if isinstance(last_sent_raw, str):
                    last_sent = datetime.fromisoformat(
                        last_sent_raw.replace("Z", "+00:00")
                    )
                    if last_sent.tzinfo is None:
                        last_sent = last_sent.replace(tzinfo=UTC)
                else:
                    continue
            except (ValueError, AttributeError):
                continue

            if last_sent < cutoff:
                hours_ago = int((datetime.now(UTC) - last_sent).total_seconds() / 3600)
                logger.warning(
                    "operational_alert: telegram_stale company_id=%s last_sent=%dh ago",
                    company.id, hours_ago,
                )
                # This is a silent operational signal — log only, no notification loop.

    except Exception:
        logger.exception("OperationalAlertsScheduler: _check_telegram_stale failed")


scheduler = OperationalAlertsScheduler()
