import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from invoice_agent.agent import AgentReply, AgentService, InvoiceTools
from invoice_agent.config import Settings
from invoice_agent.domain import InvoiceDraft, InvoiceItem
from invoice_agent.main import Services, UpdateWorker, create_app
from invoice_agent.rendering import PdfRenderer
from invoice_agent.store import Store


class PassiveWorker:
    def __init__(self) -> None:
        self.wake_count = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def wake(self) -> None:
        self.wake_count += 1


class FakeTelegram:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.sent_chat_ids: list[int] = []
        self.messages: list[tuple[str, dict[str, Any] | None]] = []
        self.edits: list[tuple[int, str]] = []
        self.documents: list[Path] = []
        self.callbacks: list[tuple[str, str | None]] = []

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.sent_chat_ids.append(chat_id)
        self.actions.append(action)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        self.sent_chat_ids.append(chat_id)
        self.messages.append((text, reply_markup))
        return {"message_id": len(self.messages)}

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> None:
        self.sent_chat_ids.append(chat_id)
        self.edits.append((message_id, text))

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.sent_chat_ids.append(chat_id)
        self.documents.append(path)

    async def answer_callback(
        self,
        callback_query_id: str,
        text: str | None = None,
    ) -> None:
        self.callbacks.append((callback_query_id, text))


@dataclass
class FakeAgent:
    reply_value: AgentReply

    async def reply(self, text: str, progress: Any = None) -> AgentReply:
        return self.reply_value


class FakeCheckpointer:
    def __init__(self) -> None:
        self.deleted_threads: list[str] = []

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted_threads.append(thread_id)


class ResetGraph:
    def __init__(self) -> None:
        self.checkpointer = FakeCheckpointer()

    async def ainvoke(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("/reset reached the LLM")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        telegram_allowed_chat_ids=(123, 456),
        telegram_webhook_secret="secret",
        openai_api_key="key",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-5.3-codex",
        database_path=tmp_path / "invoice.db",
        output_dir=tmp_path / "generated",
    )


def message_update(
    update_id: int,
    chat_id: int,
    text: str = "Create invoice",
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


@pytest.fixture
def app_context(tmp_path: Path):
    settings = make_settings(tmp_path)
    store = Store(settings.database_path)
    store.initialize(123)
    worker = PassiveWorker()
    services = Services(
        store=store,
        telegram=FakeTelegram(),
        agent=FakeAgent(AgentReply("Reply")),
        worker=worker,
    )
    app = create_app(settings, services)
    with TestClient(app) as client:
        yield client, store, worker


def test_webhook_rejects_wrong_secret(app_context) -> None:
    client, _, _ = app_context

    response = client.post(
        "/telegram/webhook",
        json=message_update(1, 123),
    )

    assert response.status_code == 403


def test_webhook_ignores_non_owner(app_context) -> None:
    client, store, worker = app_context

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json=message_update(2, 999),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert store.get_inbox(2) is None
    assert worker.wake_count == 0


def test_webhook_persists_before_return(app_context) -> None:
    client, store, worker = app_context

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret"},
        json=message_update(3, 123),
    )

    assert response.status_code == 200
    assert store.get_inbox(3).status == "pending"
    assert worker.wake_count == 1


@pytest.mark.asyncio
async def test_worker_logs_update_without_invoice_text(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store(tmp_path / "invoice.db")
    store.initialize(123)
    telegram = FakeTelegram()
    worker = UpdateWorker(
        store,
        telegram,
        {123: FakeAgent(AgentReply("Reply"))},
    )
    update = message_update(10, 123, "Private invoice details")
    store.enqueue_update(10, update)
    caplog.set_level(logging.INFO, logger="invoice_agent.main")

    await worker.process_pending()

    assert "Processing Telegram update update_id=10" in caplog.text
    assert "Processed Telegram update update_id=10" in caplog.text
    assert "Private invoice details" not in caplog.text


@pytest.mark.asyncio
async def test_worker_sends_preview_with_buttons(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = Store(settings.database_path)
    store.initialize(123)
    pdf_path = tmp_path / "preview.pdf"
    pdf_path.write_bytes(b"preview")
    telegram = FakeTelegram()
    agent = FakeAgent(AgentReply("Ready", pdf_path, 4, 2))
    worker = UpdateWorker(store, telegram, {123: agent})

    await worker.process_message(message_update(4, 123))

    assert telegram.messages == [("🔍 Reading your invoice…", None)]
    assert telegram.edits == [
        (1, "⬆️ Uploading invoice…"),
        (1, "✅ Invoice ready."),
    ]
    assert telegram.actions[0] == "typing"
    assert telegram.actions[-1] == "upload_document"
    assert telegram.documents == [pdf_path]


@pytest.mark.asyncio
async def test_reset_starts_fresh_conversation(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = Store(settings.database_path)
    store.initialize(123)
    draft = store.create_draft(123, date(2026, 8, 12))
    telegram = FakeTelegram()
    graph = ResetGraph()
    tools = InvoiceTools(store, PdfRenderer(), settings.output_dir, 123)
    agent = AgentService(graph, tools, "123")
    worker = UpdateWorker(store, telegram, {123: agent})

    await worker.process_message(message_update(5, 123, "  /RESET  "))

    assert store.get_draft(123, draft.id).status.value == "cancelled"
    assert graph.checkpointer.deleted_threads == ["123"]
    assert telegram.messages == [("Reset complete. Start a new invoice.", None)]
    assert telegram.actions == []


@pytest.mark.asyncio
async def test_current_preview_is_approved_and_returned(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    store = Store(settings.database_path)
    store.initialize(123)
    draft = store.save_draft(
        123,
        InvoiceDraft(
            invoice_number="IV-2026-0001",
            issue_date=date(2026, 8, 12),
            items=(InvoiceItem("Hosting", 1, Decimal(100)),),
        ),
    )
    pdf_path = tmp_path / "preview.pdf"
    pdf_path.write_bytes(b"exact preview")
    preview = store.save_preview(
        123,
        draft.id,
        draft.version,
        pdf_path,
        sha256(pdf_path.read_bytes()).hexdigest(),
    )
    telegram = FakeTelegram()
    worker = UpdateWorker(
        store,
        telegram,
        {123: FakeAgent(AgentReply("unused"))},
    )
    update = {
        "update_id": 8,
        "callback_query": {
            "id": "callback-1",
            "data": f"approve:{preview.id}:{preview.version}",
            "message": {"chat": {"id": 123}},
        },
    }

    await worker.process_callback(update)

    assert telegram.documents == [pdf_path]
    assert telegram.callbacks == [("callback-1", "Approved")]


@pytest.mark.asyncio
async def test_worker_routes_reply_to_source_chat(tmp_path: Path) -> None:
    store = Store(tmp_path / "invoice.db")
    store.initialize(123)
    telegram = FakeTelegram()
    worker = UpdateWorker(
        store,
        telegram,
        {456: FakeAgent(AgentReply("Second chat reply"))},
    )

    await worker.process_message(message_update(9, 456))

    assert telegram.messages == [
        ("🔍 Reading your invoice…", None),
        ("Second chat reply", None),
    ]
    assert telegram.edits == [(1, "✅ Done.")]
    assert set(telegram.sent_chat_ids) == {456}


@pytest.mark.asyncio
async def test_chat_cannot_approve_another_chats_preview(tmp_path: Path) -> None:
    store = Store(tmp_path / "invoice.db")
    store.initialize(123)
    draft = store.save_draft(
        123,
        InvoiceDraft(
            invoice_number="IV-2026-0001",
            issue_date=date(2026, 8, 12),
            items=(InvoiceItem("Hosting", 1, Decimal(100)),),
        ),
    )
    pdf_path = tmp_path / "preview.pdf"
    pdf_path.write_bytes(b"exact preview")
    preview = store.save_preview(
        123,
        draft.id,
        draft.version,
        pdf_path,
        sha256(pdf_path.read_bytes()).hexdigest(),
    )
    telegram = FakeTelegram()
    worker = UpdateWorker(
        store,
        telegram,
        {456: FakeAgent(AgentReply("unused"))},
    )
    update = {
        "callback_query": {
            "id": "callback-2",
            "data": f"approve:{preview.id}:{preview.version}",
            "message": {"chat": {"id": 456}},
        }
    }

    await worker.process_callback(update)

    assert telegram.documents == []
    assert telegram.callbacks == [("callback-2", "This action is outdated.")]
