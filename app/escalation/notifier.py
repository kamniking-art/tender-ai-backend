"""Escalation notifier — transport layer only.

Formats Telegram approval-request messages and sends them via TelegramClient.
No business logic lives here — only formatting and delivery.
"""
from __future__ import annotations

from app.telegram_notify.client import TelegramClient


async def send_approval_request(
    escalation: object,
    tender_data: dict,
    *,
    bot_token: str,
    chat_id: str | int,
) -> str | None:
    """Send a Telegram approval-request message with inline ✅ / ❌ buttons.

    Args:
        escalation:  An Escalation ORM instance (or any object with
                     ``escalation_id``, ``reason``, ``confidence`` attrs).
        tender_data: Dict with tender display fields:
                     ``subject`` (str), ``nmck`` (str|None), ``deadline`` (str|None).
        bot_token:   Telegram bot token from company profile.
        chat_id:     Telegram chat / user ID to notify.

    Returns:
        The Telegram ``message_id`` as a string, or *None* if unavailable.
    """
    subject = str(tender_data.get("subject") or "—")[:80]
    nmck = tender_data.get("nmck") or "—"
    deadline = tender_data.get("deadline") or "—"

    raw_confidence = getattr(escalation, "confidence", None)
    if raw_confidence is not None:
        confidence_pct: str = f"{int(round(float(raw_confidence) * 100))}%"
    else:
        confidence_pct = "—"

    text = (
        "🔔 Требуется решение\n\n"
        f"Тендер: {subject}\n"
        f"НМЦК: {nmck} руб.\n"
        f"Дедлайн: {deadline}\n\n"
        f"Причина: {getattr(escalation, 'reason', '—')}\n"
        f"Уверенность агента: {confidence_pct}"
    )

    escalation_id = str(getattr(escalation, "escalation_id", ""))
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Участвуем",     "callback_data": f"approve:{escalation_id}"},
                {"text": "❌ Не участвуем", "callback_data": f"reject:{escalation_id}"},
            ]
        ]
    }

    client = TelegramClient()
    try:
        msg_id = await client.send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )
        return str(msg_id) if msg_id is not None else None
    finally:
        await client.close()
