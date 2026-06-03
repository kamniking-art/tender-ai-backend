"""Clarification notifier — sends new questions to Telegram for review.

Follows the same pattern as app/escalation/notifier.py.
Step 1 — new question:  shows [✅ Одобрить] only (clarif_approve:{id})
Step 2 — after approve: message is edited to show [📤 Отправить] (clarif_send:{id})
"""
from __future__ import annotations

from app.telegram_notify.client import TelegramClient


async def send_clarification_request(
    question: object,
    tender_data: dict,
    *,
    bot_token: str,
    chat_id: str | int,
) -> str | None:
    """Send a new clarification question to Telegram with inline action buttons.

    Args:
        question:    A ClarificationQuestion ORM instance (or any object with
                     ``id``, ``question_text``, ``reason`` attrs).
        tender_data: Dict with ``subject`` (str) and optionally ``customer`` (str).
        bot_token:   Telegram bot token from company profile.
        chat_id:     Target chat / user ID.

    Returns:
        The Telegram ``message_id`` as a string, or *None* if unavailable.
    """
    subject = str(tender_data.get("subject") or "—")[:80]
    customer = tender_data.get("customer") or "—"
    question_text = str(getattr(question, "question_text", "—"))[:300]
    reason = getattr(question, "reason", None)
    question_id = str(getattr(question, "id", ""))

    text = (
        "❓ Новый вопрос на разъяснение\n\n"
        f"Тендер: {subject}\n"
        f"Заказчик: {customer}\n\n"
        f"Вопрос: {question_text}"
    )
    if reason:
        text += f"\n\nПричина: {reason[:200]}"

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "✅ Одобрить", "callback_data": f"clarif_approve:{question_id}"},
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
