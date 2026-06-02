from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

WARSAW_EXTRACTOR_URL: str = os.getenv(
    "WARSAW_EXTRACTOR_URL",
    "http://51.68.136.181:8091",
)
WARSAW_API_TOKEN: str = os.getenv("WARSAW_API_TOKEN", "")


class TelegramSendError(Exception):
    pass


class TelegramClient:
    def __init__(self, timeout_sec: int = 15) -> None:
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    async def close(self) -> None:
        await self._client.aclose()

    async def answer_callback_query(
        self,
        *,
        bot_token: str,
        callback_query_id: str,
        text: str | None = None,
    ) -> bool:
        """Dismiss the spinner on a Telegram inline button via the Warsaw proxy.

        Returns True on success, False on any error (best-effort helper).
        """
        url = f"{WARSAW_EXTRACTOR_URL}/telegram/answer_callback"
        payload: dict = {"bot_token": bot_token, "callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        headers: dict[str, str] = {}
        if WARSAW_API_TOKEN:
            headers["Authorization"] = f"Bearer {WARSAW_API_TOKEN}"
        try:
            response = await self._client.post(url, json=payload, headers=headers)
            data = response.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.warning("answer_callback_query failed: %s", exc)
            return False

    async def edit_message_reply_markup(
        self,
        *,
        bot_token: str,
        chat_id: str | int,
        message_id: int,
        reply_markup: dict | None = None,
    ) -> bool:
        """Remove or replace the inline keyboard on a sent message via Warsaw proxy.

        Pass reply_markup={} (or None) to remove the keyboard entirely.
        Returns True on success, False on any error (best-effort helper).
        """
        url = f"{WARSAW_EXTRACTOR_URL}/telegram/edit_reply_markup"
        payload: dict = {
            "bot_token": bot_token,
            "chat_id": str(chat_id),
            "message_id": message_id,
            "reply_markup": reply_markup if reply_markup is not None else {},
        }
        headers: dict[str, str] = {}
        if WARSAW_API_TOKEN:
            headers["Authorization"] = f"Bearer {WARSAW_API_TOKEN}"
        try:
            response = await self._client.post(url, json=payload, headers=headers)
            data = response.json()
            return bool(data.get("ok"))
        except Exception as exc:
            logger.warning("edit_message_reply_markup failed: %s", exc)
            return False

    async def send_message(
        self,
        *,
        bot_token: str,
        chat_id: str | int,
        text: str,
        reply_markup: dict | None = None,
    ) -> int | None:
        """Send a Telegram message via the Warsaw extractor proxy.

        Args:
            bot_token:    Telegram bot token.
            chat_id:      Target chat / user ID.
            text:         Message text (plain or HTML).
            reply_markup: Optional inline keyboard or other reply markup dict.

        Returns:
            The ``message_id`` from Telegram's response, or *None* on failure.
        """
        # Warsaw sends with parse_mode=HTML — escape bare < > & so they render
        # as literal characters instead of broken HTML tags.
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        url = f"{WARSAW_EXTRACTOR_URL}/telegram/send"
        payload: dict = {
            "bot_token": bot_token,
            "chat_id": str(chat_id),
            "text": safe_text,
            "reply_markup": reply_markup,
        }
        headers: dict[str, str] = {}
        if WARSAW_API_TOKEN:
            headers["Authorization"] = f"Bearer {WARSAW_API_TOKEN}"

        delays = [0, 1, 3]
        last_error: str | None = None

        for attempt, delay in enumerate(delays, start=1):
            if delay:
                logger.warning(
                    "retry: provider=telegram error=%s attempt=%d delay=%.1fs",
                    last_error,
                    attempt,
                    float(delay),
                )
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, json=payload, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"proxy http {response.status_code}"
                    continue
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    _tg_err = data.get("error") or "telegram send failed"
                    logger.warning(
                        "telegram send failed: provider=telegram attempt=%d error=%s response=%s",
                        attempt,
                        _tg_err,
                        data,
                    )
                    raise TelegramSendError(_tg_err)
                return data.get("message_id")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                continue
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "telegram send failed: provider=telegram attempt=%d status=%d body=%.200s",
                    attempt,
                    exc.response.status_code,
                    exc.response.text,
                )
                raise TelegramSendError(f"proxy http {exc.response.status_code}") from exc

        logger.warning(
            "retry: provider=telegram exhausted attempts=%d last_error=%s",
            len(delays),
            last_error,
        )
        raise TelegramSendError(last_error or "telegram send failed")
