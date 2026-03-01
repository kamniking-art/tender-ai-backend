from __future__ import annotations

import asyncio

import httpx


class TelegramSendError(Exception):
    pass


class TelegramClient:
    def __init__(self, timeout_sec: int = 15) -> None:
        self._client = httpx.AsyncClient(timeout=timeout_sec)

    async def close(self) -> None:
        await self._client.aclose()

    async def send_message(self, *, bot_token: str, chat_id: str | int, text: str) -> None:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }

        delays = [0, 1, 3]
        last_error: str | None = None

        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await self._client.post(url, json=payload)
                if response.status_code in (429, 500, 502, 503, 504):
                    last_error = f"telegram http {response.status_code}"
                    continue
                response.raise_for_status()
                data = response.json()
                if not data.get("ok", False):
                    description = data.get("description", "unknown telegram error")
                    raise TelegramSendError(description)
                return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = str(exc)
                continue
            except httpx.HTTPStatusError as exc:
                raise TelegramSendError(f"telegram http {exc.response.status_code}") from exc

        raise TelegramSendError(last_error or "telegram send failed")
