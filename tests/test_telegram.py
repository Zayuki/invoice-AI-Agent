import asyncio
import json
from pathlib import Path

import httpx
import pytest

from invoice_agent.telegram import TelegramClient, WorkIndicator


class FakeTelegram:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.refreshed = asyncio.Event()

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions.append(action)
        if len(self.actions) >= 2:
            self.refreshed.set()


class RequestRecorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})


async def raise_runtime_error() -> None:
    raise RuntimeError("Event loop is closed")


@pytest.mark.asyncio
async def test_indicator_refreshes_changes_action_and_stops() -> None:
    telegram = FakeTelegram()
    indicator = WorkIndicator(telegram, 123, interval=0.01)

    await indicator.start("typing")
    await asyncio.wait_for(telegram.refreshed.wait(), timeout=0.2)
    await indicator.set_action("upload_document")
    await indicator.stop()

    assert telegram.actions[:2] == ["typing", "typing"]
    assert telegram.actions[-1] == "upload_document"
    assert indicator.is_running is False


@pytest.mark.asyncio
async def test_indicator_stop_ignores_background_failure() -> None:
    indicator = WorkIndicator(FakeTelegram(), 123)
    indicator.task = asyncio.create_task(raise_runtime_error())
    await asyncio.sleep(0)

    await indicator.stop()

    assert indicator.is_running is False


@pytest.mark.asyncio
async def test_document_uses_multipart(tmp_path: Path) -> None:
    recorder = RequestRecorder()
    transport = httpx.MockTransport(recorder)
    pdf_path = tmp_path / "invoice.pdf"
    pdf_path.write_bytes(b"pdf bytes")
    client = TelegramClient("token", transport=transport)

    await client.send_document(
        123,
        pdf_path,
        "Invoice preview",
        {"inline_keyboard": [[{"text": "Approve", "callback_data": "a"}]]},
    )
    await client.close()

    request = recorder.requests[-1]
    body = request.content
    assert request.url.path.endswith("/sendDocument")
    assert b"invoice.pdf" in body
    assert b"pdf bytes" in body
    assert b"Invoice preview" in body


@pytest.mark.asyncio
async def test_message_serializes_reply_markup() -> None:
    recorder = RequestRecorder()
    client = TelegramClient("token", transport=httpx.MockTransport(recorder))
    keyboard = {"inline_keyboard": [[{"text": "Retry", "callback_data": "r:1"}]]}

    await client.send_message(123, "Try again", keyboard)
    await client.close()

    payload = json.loads(recorder.requests[-1].content)
    assert payload["chat_id"] == 123
    assert payload["reply_markup"] == keyboard


@pytest.mark.asyncio
async def test_message_edit_uses_existing_message() -> None:
    recorder = RequestRecorder()
    client = TelegramClient("token", transport=httpx.MockTransport(recorder))

    await client.edit_message(123, 7, "Generating PDF preview…")
    await client.close()

    payload = json.loads(recorder.requests[-1].content)
    assert recorder.requests[-1].url.path.endswith("/editMessageText")
    assert payload == {
        "chat_id": 123,
        "message_id": 7,
        "text": "Generating PDF preview…",
    }
