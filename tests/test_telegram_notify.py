from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from uuid import uuid4

from app.tender_alerts.schemas import AlertCategory, AlertTenderItem
from app.telegram_notify.service import (
    dedup_unsent_items,
    is_min_interval_elapsed,
    process_company_notifications,
    within_send_window,
)


class TelegramNotifyUnitTests(TestCase):
    def test_dedup_skips_already_sent(self) -> None:
        t1 = uuid4()
        t2 = uuid4()
        items = [
            AlertTenderItem(tender_id=t1, category=AlertCategory.NEW),
            AlertTenderItem(tender_id=t2, category=AlertCategory.NEW),
        ]
        state = {
            "sent": {
                "new": {
                    f"new:{t1}:{datetime.now(UTC).strftime('%Y-%m-%d')}": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                }
            }
        }

        unsent = dedup_unsent_items("new", items, state)
        self.assertEqual(len(unsent), 1)
        self.assertEqual(unsent[0].tender_id, t2)

    def test_send_window_logic(self) -> None:
        now = datetime(2026, 3, 1, 10, 0, 0)
        self.assertTrue(within_send_window("09:00", "21:00", now_local=now))
        self.assertFalse(within_send_window("11:00", "21:00", now_local=now))
        self.assertTrue(within_send_window("22:00", "06:00", now_local=datetime(2026, 3, 1, 23, 0, 0)))

    def test_min_interval(self) -> None:
        now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        recent = (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        old = (now - timedelta(minutes=40)).isoformat().replace("+00:00", "Z")
        self.assertFalse(is_min_interval_elapsed(recent, 30, now_utc=now))
        self.assertTrue(is_min_interval_elapsed(old, 30, now_utc=now))


class _FakeDB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self):
        self.commits += 1


class _FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, *, bot_token: str, chat_id: str | int, text: str) -> None:
        self.messages.append(text)


class TelegramNotifyIntegrationTests(IsolatedAsyncioTestCase):
    async def test_process_company_notifications_updates_profile_state(self) -> None:
        company_id = uuid4()
        tender_id = uuid4()
        company = SimpleNamespace(
            id=company_id,
            profile={
                "telegram": {
                    "enabled": True,
                    "bot_token": "token",
                    "chat_id": "123",
                    "min_interval_minutes": 0,
                },
                "telegram_state": {
                    "last_sent_at": None,
                    "sent": {
                        "new": {},
                        "deadline_24h": {},
                        "risky": {},
                    },
                },
            },
        )
        db = _FakeDB()
        client = _FakeTelegramClient()

        from app.telegram_notify import service as notify_service

        old_alerts = notify_service._get_alert_items_for_company
        old_nmck = notify_service._get_nmck_map
        old_flags = notify_service._get_risk_flags_map
        try:
            async def _mock_alerts(_db, _company_id, category):
                if category == AlertCategory.NEW:
                    return [
                        AlertTenderItem(
                            tender_id=tender_id,
                            category=AlertCategory.NEW,
                            title="Tender A",
                            deadline_at=datetime.now(UTC) + timedelta(hours=20),
                            risk_score=75,
                            recommendation="unsure",
                        )
                    ]
                if category == AlertCategory.DEADLINE_SOON:
                    return [
                        AlertTenderItem(
                            tender_id=tender_id,
                            category=AlertCategory.DEADLINE_SOON,
                            title="Tender A",
                            deadline_at=datetime.now(UTC) + timedelta(hours=20),
                            risk_score=75,
                            recommendation="unsure",
                        )
                    ]
                if category == AlertCategory.RISKY:
                    return [
                        AlertTenderItem(
                            tender_id=tender_id,
                            category=AlertCategory.RISKY,
                            title="Tender A",
                            deadline_at=datetime.now(UTC) + timedelta(hours=20),
                            risk_score=75,
                            recommendation="unsure",
                        )
                    ]
                return []

            async def _mock_nmck(_db, _company_id, _ids):
                return {tender_id: 1000000}

            async def _mock_flags(_db, _company_id, _ids):
                return {tender_id: ["short_deadline"]}

            notify_service._get_alert_items_for_company = _mock_alerts
            notify_service._get_nmck_map = _mock_nmck
            notify_service._get_risk_flags_map = _mock_flags

            stats = await process_company_notifications(db, company, client)
        finally:
            notify_service._get_alert_items_for_company = old_alerts
            notify_service._get_nmck_map = old_nmck
            notify_service._get_risk_flags_map = old_flags

        self.assertEqual(stats.sent_messages, 3)
        self.assertEqual(len(client.messages), 3)
        self.assertEqual(db.commits, 1)

        state = company.profile.get("telegram_state")
        self.assertIsNotNone(state)
        self.assertTrue(state.get("last_sent_at"))
        sent = state.get("sent", {})
        self.assertTrue(any(str(tender_id) in key for key in sent.get("new", {}).keys()))
        self.assertTrue(any(str(tender_id) in key for key in sent.get("deadline_24h", {}).keys()))
        self.assertTrue(any(str(tender_id) in key for key in sent.get("risky", {}).keys()))
