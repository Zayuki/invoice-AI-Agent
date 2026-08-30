import asyncio
import logging
import secrets
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from time import monotonic
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from invoice_agent.agent import (
    INITIAL_STATUS,
    AgentService,
    InvoiceTools,
    ToolProgress,
    build_agent,
)
from invoice_agent.config import Settings
from invoice_agent.rendering import PdfRenderer
from invoice_agent.store import InboxUpdate, StalePreviewError, Store, payload_chat_id
from invoice_agent.telegram import TelegramAPIError, TelegramClient, WorkIndicator

LOGGER = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
RETRY_DELAY_SECONDS = 2.0
TRANSIENT_ERRORS = (httpx.HTTPError, TelegramAPIError)


@dataclass
class Services:
    store: Store
    telegram: Any
    agents: dict[int, Any]
    worker: Any


class UpdateWorker:
    def __init__(
        self,
        store: Store,
        telegram: Any,
        chat_id: int,
        agent: Any,
    ) -> None:
        self.store = store
        self.telegram = telegram
        self.chat_id = chat_id
        self.agent = agent
        self.signal = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.task is not None:
            return
        self.task = asyncio.create_task(self.run())
        self.wake()

    async def stop(self) -> None:
        if self.task is None:
            return
        self.task.cancel()
        with suppress(asyncio.CancelledError):
            await self.task
        self.task = None

    def wake(self) -> None:
        self.signal.set()

    async def run(self) -> None:
        while True:
            await self.signal.wait()
            self.signal.clear()
            await self.process_pending()

    async def process_pending(self) -> None:
        while update := self.store.claim_next_update(self.chat_id):
            started = monotonic()
            update_kind = (
                "callback" if "callback_query" in update.payload else "message"
            )
            LOGGER.info(
                "Processing Telegram update update_id=%s chat_id=%s kind=%s attempt=%s",
                update.update_id,
                self.chat_id,
                update_kind,
                update.attempts,
            )
            try:
                await self.process_update(update.payload)
                self.store.complete_update(update.update_id)
                LOGGER.info(
                    "Processed Telegram update update_id=%s duration_ms=%.0f",
                    update.update_id,
                    (monotonic() - started) * 1000,
                )
            except TRANSIENT_ERRORS as error:
                if update_kind == "message" and update.attempts < MAX_ATTEMPTS:
                    LOGGER.warning(
                        "Retrying Telegram update update_id=%s after %s",
                        update.update_id,
                        type(error).__name__,
                    )
                    self.store.retry_update(update.update_id)
                    await asyncio.sleep(RETRY_DELAY_SECONDS * update.attempts)
                    continue
                await self.report_failure(update, error, started)
            except Exception as error:  # noqa: BLE001
                await self.report_failure(update, error, started)

    async def report_failure(
        self,
        update: InboxUpdate,
        error: Exception,
        started: float,
    ) -> None:
        LOGGER.exception(
            "Telegram update failed update_id=%s chat_id=%s duration_ms=%.0f",
            update.update_id,
            self.chat_id,
            (monotonic() - started) * 1000,
        )
        self.store.fail_update(update.update_id, type(error).__name__)
        with suppress(Exception):
            await self.telegram.send_message(
                self.chat_id,
                "Something went wrong. Try again?",
                retry_keyboard(update.update_id),
            )

    async def process_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self.process_callback(update)
            return
        await self.process_message(update)

    async def process_message(self, update: dict[str, Any]) -> None:
        chat_id = self.chat_id
        agent = self.agent
        text = update.get("message", {}).get("text")
        if not text:
            await self.telegram.send_message(
                chat_id,
                "Please send invoice details as text.",
            )
            return
        if text.strip().lower() == "/reset":
            await agent.reset()
            await self.telegram.send_message(
                chat_id,
                "Reset complete. Start a new invoice.",
            )
            return
        indicator = WorkIndicator(self.telegram, chat_id)
        status = INITIAL_STATUS
        message = await self.telegram.send_message(chat_id, status)
        progress = ToolProgress(
            partial(self.telegram.edit_message, chat_id, message["message_id"]),
            status,
        )
        await indicator.start("typing")
        try:
            reply = await agent.reply(text, progress)
            if reply.pdf_path:
                await progress.set_status("⬆️ Uploading invoice…")
                await indicator.set_action("upload_document")
                await self.telegram.send_document(
                    chat_id,
                    reply.pdf_path,
                    reply.text,
                    preview_keyboard(reply.draft_id, reply.version),
                )
                await progress.set_status("✅ Invoice ready.")
                return
            await progress.set_status("✅ Done.")
            await self.telegram.send_message(chat_id, reply.text)
        finally:
            await indicator.stop()

    async def process_callback(self, update: dict[str, Any]) -> None:
        chat_id = self.chat_id
        callback = update["callback_query"]
        callback_id = callback["id"]
        data = callback.get("data", "")
        try:
            action, identifier, version = parse_callback(data)
            if action == "approve":
                await self.approve(callback_id, identifier, version)
            elif action == "edit":
                self.store.reopen_draft(chat_id, identifier, version)
                await self.telegram.answer_callback(callback_id, "Editing")
                await self.telegram.send_message(
                    chat_id,
                    "What should I change?",
                )
            elif action == "cancel":
                self.store.cancel_draft(chat_id, identifier, version)
                await self.telegram.answer_callback(callback_id, "Cancelled")
                await self.telegram.send_message(chat_id, "Invoice cancelled.")
            elif action == "retry":
                failed = self.store.get_inbox(identifier)
                if failed is None or failed.chat_id != self.chat_id:
                    raise KeyError("Update belongs to another chat")
                self.store.retry_update(identifier)
                await self.telegram.answer_callback(callback_id, "Retrying")
                self.wake()
            else:
                raise ValueError("Unknown callback")
        except (ValueError, KeyError, StalePreviewError):
            await self.telegram.answer_callback(callback_id, "This action is outdated.")

    async def approve(
        self,
        callback_id: str,
        draft_id: int,
        version: int,
    ) -> None:
        chat_id = self.chat_id
        path = self.store.approve_preview(chat_id, draft_id, version)
        await self.telegram.answer_callback(callback_id, "Approved")
        indicator = WorkIndicator(self.telegram, chat_id)
        await indicator.start("upload_document")
        try:
            await self.telegram.send_document(
                chat_id,
                path,
                "Approved invoice. Forward it manually to your customer.",
            )
            with suppress(Exception):
                await self.agent.clear_thread()
        finally:
            await indicator.stop()


def parse_callback(data: str) -> tuple[str, int, int]:
    parts = data.split(":")
    if len(parts) == 2 and parts[0] == "retry":
        return parts[0], int(parts[1]), 0
    if len(parts) != 3:
        raise ValueError("Invalid callback")
    return parts[0], int(parts[1]), int(parts[2])


def preview_keyboard(draft_id: int, version: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve",
                    "callback_data": f"approve:{draft_id}:{version}",
                },
                {"text": "Edit", "callback_data": f"edit:{draft_id}:{version}"},
            ],
            [
                {
                    "text": "Cancel",
                    "callback_data": f"cancel:{draft_id}:{version}",
                }
            ],
        ]
    }


def retry_keyboard(update_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[{"text": "Retry", "callback_data": f"retry:{update_id}"}]]
    }


class WorkerPool:
    def __init__(self, workers: dict[int, UpdateWorker]) -> None:
        self.workers = workers

    async def start(self) -> None:
        for worker in self.workers.values():
            await worker.start()

    async def stop(self) -> None:
        for worker in self.workers.values():
            await worker.stop()

    def wake(self, chat_id: int) -> None:
        worker = self.workers.get(chat_id)
        if worker is not None:
            worker.wake()


async def build_services(settings: Settings, stack: Any) -> Services:
    store = Store(settings.database_path)
    store.initialize(settings.telegram_allowed_chat_ids[0])
    store.delete_cancelled_drafts_before(datetime.now(UTC) - timedelta(days=30))
    checkpoint_path = settings.database_path.with_suffix(".checkpoints.db")
    checkpointer = await stack.enter_async_context(
        AsyncSqliteSaver.from_conn_string(str(checkpoint_path))
    )
    await checkpointer.setup()
    telegram = TelegramClient(settings.telegram_bot_token)
    stack.push_async_callback(telegram.close)
    renderer = PdfRenderer()
    stack.push_async_callback(renderer.close)
    agents: dict[int, AgentService] = {}
    workers: dict[int, UpdateWorker] = {}
    for chat_id in settings.telegram_allowed_chat_ids:
        invoice_tools = InvoiceTools(store, renderer, settings.output_dir, chat_id)
        graph = build_agent(settings, invoice_tools, checkpointer)
        agents[chat_id] = AgentService(graph, invoice_tools, str(chat_id))
        workers[chat_id] = UpdateWorker(store, telegram, chat_id, agents[chat_id])
    return Services(store, telegram, agents, WorkerPool(workers))


@asynccontextmanager
async def application_lifespan(app: FastAPI):
    async with AsyncExitStack() as stack:
        settings = app.state.settings
        active = app.state.configured_services
        if active is None:
            active = await build_services(settings, stack)
        app.state.services = active
        app.state.store = active.store
        app.state.worker = active.worker
        await active.worker.start()
        LOGGER.info("Invoice Agent started")
        try:
            yield
        finally:
            await active.worker.stop()
            LOGGER.info("Invoice Agent stopped")


async def telegram_webhook(request: Request) -> dict[str, bool]:
    settings = request.app.state.settings
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not secrets.compare_digest(secret or "", settings.telegram_webhook_secret):
        LOGGER.warning("Rejected Telegram webhook with invalid secret")
        raise HTTPException(status_code=403)
    update = await request.json()
    update_id = update.get("update_id")
    chat_id = payload_chat_id(update)
    if chat_id not in settings.telegram_allowed_chat_ids:
        LOGGER.info("Ignored Telegram update update_id=%s", update_id)
        return {"ok": True}
    if not isinstance(update_id, int):
        LOGGER.warning("Rejected Telegram webhook with invalid update ID")
        raise HTTPException(status_code=400, detail="Invalid update")
    inserted = request.app.state.store.enqueue_update(update_id, chat_id, update)
    if inserted:
        LOGGER.info("Queued Telegram update update_id=%s", update_id)
        request.app.state.worker.wake(chat_id)
    else:
        LOGGER.info("Ignored duplicate Telegram update update_id=%s", update_id)
    return {"ok": True}


async def health() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    settings: Settings,
    services: Services | None = None,
) -> FastAPI:
    app = FastAPI(title="Invoice Agent", lifespan=application_lifespan)
    app.state.settings = settings
    app.state.configured_services = services
    app.get("/health")(health)
    app.post("/telegram/webhook")(telegram_webhook)
    return app


def create_configured_app() -> FastAPI:
    settings = Settings.from_env()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return create_app(settings)
