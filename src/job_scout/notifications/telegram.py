"""Telegram transport with explicit outcomes and no token-bearing error messages."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

import httpx

from job_scout.notifications.digest import MAX_UNITS, units


class DeliveryError(Exception):
    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class TelegramSender:
    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        chat_id: str,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
            raise ValueError("Invalid TELEGRAM_BOT_TOKEN format")
        if not re.fullmatch(r"-?\d+", chat_id):
            raise ValueError("TELEGRAM_CHAT_ID must be a numeric chat ID")
        self.client, self.token, self.chat_id, self.sleep = client, token, chat_id, sleep
        # HTTPX INFO logs include the token in the endpoint path.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

    async def send(self, text: str) -> int:
        if not text or units(text) > MAX_UNITS:
            raise ValueError("Invalid Telegram message length")
        for attempt in range(3):
            try:
                response = await self.client.post(
                    f"https://api.telegram.org/bot{self.token}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "link_preview_options": {"is_disabled": True},
                    },
                    timeout=20.0,
                    follow_redirects=False,
                )
            except httpx.TransportError:
                raise DeliveryError(
                    "Telegram transport error; delivery outcome is uncertain", uncertain=True
                ) from None
            try:
                body = response.json()
            except ValueError:
                raise DeliveryError(
                    "Telegram returned an unreadable response; outcome uncertain", uncertain=True
                ) from None
            if not isinstance(body, dict):
                raise DeliveryError("Telegram returned an invalid response", uncertain=True)
            if response.status_code == 200 and body.get("ok") is True:
                result = body.get("result")
                if (
                    isinstance(result, dict)
                    and type(result.get("message_id")) is int
                    and result["message_id"] > 0
                    and isinstance(result.get("chat"), dict)
                    and result["chat"].get("id") == int(self.chat_id)
                ):
                    return result["message_id"]
                raise DeliveryError("Telegram confirmation is incomplete", uncertain=True)
            if response.status_code == 429 and body.get("ok") is False:
                parameters = body.get("parameters")
                delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
                if type(delay) is int and 0 <= delay <= 30 and attempt < 2:
                    await self.sleep(max(1, delay))
                    continue
                raise DeliveryError("Telegram rate limited this request; retry later")
            if 400 <= response.status_code < 500 and body.get("ok") is False:
                raise DeliveryError(f"Telegram rejected the request (HTTP {response.status_code})")
            raise DeliveryError(
                "Telegram server response is uncertain; inspect chat before retrying",
                uncertain=True,
            )
        raise AssertionError("Unreachable")

    async def private_chats(self) -> list[int]:
        """Read recent bot updates to discover private chat IDs; never send messages."""
        try:
            response = await self.client.get(
                f"https://api.telegram.org/bot{self.token}/getUpdates",
                params={"timeout": 0, "limit": 100},
                timeout=20.0,
                follow_redirects=False,
            )
            body = response.json()
        except (httpx.TransportError, ValueError):
            raise DeliveryError(
                "Could not read Telegram updates; check token and connection"
            ) from None
        if response.status_code != 200 or not isinstance(body, dict) or body.get("ok") is not True:
            raise DeliveryError("Telegram rejected getUpdates; check token or an existing webhook")
        updates = body.get("result")
        if not isinstance(updates, list):
            raise DeliveryError("Invalid Telegram updates response")
        chats = set()
        for update in updates:
            message = update.get("message") if isinstance(update, dict) else None
            chat = message.get("chat") if isinstance(message, dict) else None
            if (
                isinstance(chat, dict)
                and chat.get("type") == "private"
                and type(chat.get("id")) is int
            ):
                chats.add(chat["id"])
        return sorted(chats)
