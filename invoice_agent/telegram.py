import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

import httpx


class TelegramAPIError(RuntimeError):
    pass


class ChatActionSender(Protocol):
    async def send_chat_action(self, chat_id: int, action: str) -> None: ...


class TelegramClient:
    def __init__(
        self,
        token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}",
            timeout=httpx.Timeout(30, connect=10),
            transport=transport or httpx.AsyncHTTPTransport(retries=2),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> Any:
        if files:
            data = {
                key: json.dumps(value) if isinstance(value, dict) else str(value)
                for key, value in (payload or {}).items()
            }
            response = await self.client.post(method, data=data, files=files)
        else:
            response = await self.client.post(method, json=payload or {})
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise TelegramAPIError(body.get("description", "Telegram API failed"))
        return body.get("result")

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return await self.call("sendMessage", payload)

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> Any:
        return await self.call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text},
        )

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        files = {"document": (path.name, path.read_bytes(), "application/pdf")}
        return await self.call("sendDocument", payload, files)

    async def send_chat_action(self, chat_id: int, action: str) -> Any:
        return await self.call(
            "sendChatAction",
            {"chat_id": chat_id, "action": action},
        )

    async def answer_callback(
        self,
        callback_query_id: str,
        text: str | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return await self.call("answerCallbackQuery", payload)


class WorkIndicator:
    def __init__(
        self,
        telegram: ChatActionSender,
        chat_id: int,
        interval: float = 4,
    ) -> None:
        self.telegram = telegram
        self.chat_id = chat_id
        self.interval = interval
        self.action = "typing"
        self.task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start(self, action: str = "typing") -> None:
        if self.is_running:
            await self.set_action(action)
            return
        self.action = action
        await self.send_action()
        self.task = asyncio.create_task(self.run())

    async def set_action(self, action: str) -> None:
        self.action = action
        await self.send_action()

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            await self.send_action()

    async def send_action(self) -> None:
        with suppress(TelegramAPIError, httpx.HTTPError):
            await self.telegram.send_chat_action(self.chat_id, self.action)

    async def stop(self) -> None:
        task = self.task
        self.task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
