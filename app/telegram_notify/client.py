from __future__ import annotations

import asyncio
import os

import httpx

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
        url = f"{WARSAW_EXTRACTOR_URL}/telegram/send"
        payload: dict = {
            "bot_token": bot_token,
            "chat_id": str(chat_id),
            "text": text,
            "reply_markup": reply_markup,
        }
        headers: dict[str, str] = {}
        if WARSAW_API_TOKEN:
            headers["Authorization"] = f"Bearer {WARSAW_API_TOKEN}"

        delays = [0, 1, 3]
        last_error: str | None = None

        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, json=payload, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"proxy http {response.status_code}"
                    continue
                response.raise_for_status()
                data = response.json()
                if not data.get("ok"):
                    raise TelegramSendError(data.get("error") or "telegram send failed")
                return data.get("message_id")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = str(exc)
                continue
            except httpx.HTTPStatusError as exc:
                raise TelegramSendError(f"proxy http {exc.response.status_code}") from exc

        raise TelegramSendError(last_error or "telegram send failed")
