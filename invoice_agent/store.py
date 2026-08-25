import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any

from invoice_agent.domain import DraftStatus, InvoiceDraft, InvoiceItem


class StalePreviewError(ValueError):
    pass


class InvalidTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class InboxUpdate:
    update_id: int
    payload: dict[str, Any]
    status: str
    error: str | None = None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self, legacy_chat_id: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    update_id INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                """
            )
            columns = connection.execute("PRAGMA table_info(drafts)").fetchall()
            if not columns:
                self.create_drafts_table(connection)
            elif "chat_id" not in {column["name"] for column in columns}:
                self.migrate_drafts(connection, legacy_chat_id)
            elif "cancelled_at" not in {column["name"] for column in columns}:
                connection.execute("ALTER TABLE drafts ADD COLUMN cancelled_at TEXT")
            connection.execute(
                "UPDATE drafts SET cancelled_at = ? "
                "WHERE status = 'cancelled' AND cancelled_at IS NULL",
                (datetime.now(UTC).isoformat(),),
            )
            connection.execute(
                "UPDATE inbox SET status = 'pending' WHERE status = 'processing'"
            )

    def create_drafts_table(
        self,
        connection: sqlite3.Connection,
        table: str = "drafts",
    ) -> None:
        connection.execute(
            f"""CREATE TABLE {table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                invoice_number TEXT NOT NULL,
                issue_date TEXT NOT NULL,
                data TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                preview_path TEXT,
                preview_digest TEXT,
                cancelled_at TEXT,
                UNIQUE(chat_id, invoice_number)
            )"""
        )

    def migrate_drafts(
        self,
        connection: sqlite3.Connection,
        legacy_chat_id: int,
    ) -> None:
        self.create_drafts_table(connection, "drafts_new")
        connection.execute(
            "INSERT INTO drafts_new "
            "SELECT id, ?, invoice_number, issue_date, data, version, status, "
            "preview_path, preview_digest, NULL FROM drafts",
            (legacy_chat_id,),
        )
        connection.execute("DROP TABLE drafts")
        connection.execute("ALTER TABLE drafts_new RENAME TO drafts")
        connection.execute(
            "UPDATE counters SET name = ? WHERE name = 'invoice'",
            (f"invoice:{legacy_chat_id}",),
        )

    def enqueue_update(self, update_id: int, payload: dict[str, Any]) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbox(update_id, payload, status) "
                "VALUES (?, ?, 'pending')",
                (update_id, json.dumps(payload, ensure_ascii=False)),
            )
            return cursor.rowcount == 1

    def claim_next_update(self) -> InboxUpdate | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM inbox WHERE status = 'pending' "
                "ORDER BY update_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE inbox SET status = 'processing', error = NULL "
                "WHERE update_id = ?",
                (row["update_id"],),
            )
            return self.inbox_from_row(row, status="processing")

    def complete_update(self, update_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET status = 'done', error = NULL WHERE update_id = ?",
                (update_id,),
            )

    def fail_update(self, update_id: int, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET status = 'failed', error = ? WHERE update_id = ?",
                (error, update_id),
            )

    def retry_update(self, update_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE inbox SET status = 'pending', error = NULL WHERE update_id = ?",
                (update_id,),
            )

    def get_inbox(self, update_id: int) -> InboxUpdate | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbox WHERE update_id = ?",
                (update_id,),
            ).fetchone()
        return self.inbox_from_row(row) if row else None

    def create_draft(self, chat_id: int, issue_date: date) -> InvoiceDraft:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            while True:
                sequence = self.next_invoice_sequence(connection, chat_id)
                invoice_number = f"IV-{issue_date.year}-{sequence:04d}"
                exists = connection.execute(
                    "SELECT 1 FROM drafts WHERE chat_id = ? AND invoice_number = ?",
                    (chat_id, invoice_number),
                ).fetchone()
                if exists is None:
                    break
            draft = InvoiceDraft(invoice_number=invoice_number, issue_date=issue_date)
            cursor = connection.execute(
                "INSERT INTO drafts(chat_id, invoice_number, issue_date, data, "
                "version, status) VALUES (?, ?, ?, ?, 0, ?)",
                (
                    chat_id,
                    invoice_number,
                    issue_date.isoformat(),
                    self.serialize_draft(draft),
                    DraftStatus.COLLECTING,
                ),
            )
            return replace(draft, id=cursor.lastrowid)

    def get_or_create_draft(self, chat_id: int, issue_date: date) -> InvoiceDraft:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE status IN ('collecting', 'previewed') "
                "AND chat_id = ? ORDER BY id DESC LIMIT 1",
                (chat_id,),
            ).fetchone()
        return (
            self.draft_from_row(row) if row else self.create_draft(chat_id, issue_date)
        )

    def save_draft(self, chat_id: int, draft: InvoiceDraft) -> InvoiceDraft:
        if draft.id is None:
            return self.insert_draft(chat_id, draft)
        current = self.get_draft(chat_id, draft.id)
        if current.status in (DraftStatus.APPROVED, DraftStatus.CANCELLED):
            raise InvalidTransitionError(f"Cannot edit {current.status} draft")
        updated = replace(
            draft,
            version=current.version + 1,
            status=DraftStatus.COLLECTING,
            preview_path=None,
            preview_digest=None,
        )
        with self.connect() as connection:
            connection.execute(
                "UPDATE drafts SET invoice_number = ?, issue_date = ?, data = ?, "
                "version = ?, status = ?, "
                "preview_path = NULL, preview_digest = NULL "
                "WHERE id = ? AND chat_id = ?",
                (
                    updated.invoice_number,
                    updated.issue_date.isoformat(),
                    self.serialize_draft(updated),
                    updated.version,
                    updated.status,
                    updated.id,
                    chat_id,
                ),
            )
        return updated

    def insert_draft(self, chat_id: int, draft: InvoiceDraft) -> InvoiceDraft:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO drafts(chat_id, invoice_number, issue_date, data, "
                "version, status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    draft.invoice_number,
                    draft.issue_date.isoformat(),
                    self.serialize_draft(draft),
                    draft.version,
                    draft.status,
                ),
            )
        return replace(draft, id=cursor.lastrowid)

    def get_draft(self, chat_id: int, draft_id: int) -> InvoiceDraft:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM drafts WHERE id = ? AND chat_id = ?",
                (draft_id, chat_id),
            ).fetchone()
        if row is None:
            raise KeyError(f"Draft {draft_id} does not exist")
        return self.draft_from_row(row)

    def save_preview(
        self,
        chat_id: int,
        draft_id: int,
        version: int,
        path: Path,
        digest: str,
    ) -> InvoiceDraft:
        current = self.get_draft(chat_id, draft_id)
        if current.version != version or current.status != DraftStatus.COLLECTING:
            raise StalePreviewError("Draft changed before preview was saved")
        with self.connect() as connection:
            connection.execute(
                "UPDATE drafts SET status = 'previewed', preview_path = ?, "
                "preview_digest = ? WHERE id = ? AND chat_id = ? AND version = ?",
                (str(path), digest, draft_id, chat_id, version),
            )
        return replace(
            current,
            status=DraftStatus.PREVIEWED,
            preview_path=str(path),
            preview_digest=digest,
        )

    def approve_preview(self, chat_id: int, draft_id: int, version: int) -> Path:
        current = self.get_draft(chat_id, draft_id)
        if (
            current.version != version
            or current.status != DraftStatus.PREVIEWED
            or not current.preview_path
            or not current.preview_digest
        ):
            raise StalePreviewError("Preview is no longer current")
        path = Path(current.preview_path)
        digest = sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if digest != current.preview_digest:
            raise StalePreviewError("Preview bytes no longer match the review")
        with self.connect() as connection:
            connection.execute(
                "UPDATE drafts SET status = 'approved' WHERE id = ? AND chat_id = ?",
                (draft_id, chat_id),
            )
        return path

    def cancel_draft(
        self,
        chat_id: int,
        draft_id: int,
        version: int | None = None,
    ) -> None:
        current = self.get_draft(chat_id, draft_id)
        if version is not None and (
            current.version != version or current.status != DraftStatus.PREVIEWED
        ):
            raise StalePreviewError("Preview is no longer current")
        if current.status == DraftStatus.APPROVED:
            raise InvalidTransitionError("Approved drafts cannot be cancelled")
        with self.connect() as connection:
            connection.execute(
                "UPDATE drafts SET status = 'cancelled', cancelled_at = ? "
                "WHERE id = ? AND chat_id = ?",
                (datetime.now(UTC).isoformat(), draft_id, chat_id),
            )

    def cancel_active_draft(self, chat_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE drafts SET status = 'cancelled', cancelled_at = ? WHERE id = ("
                "SELECT id FROM drafts "
                "WHERE status IN ('collecting', 'previewed') AND chat_id = ? "
                "ORDER BY id DESC LIMIT 1"
                ") AND status IN ('collecting', 'previewed') AND chat_id = ?",
                (datetime.now(UTC).isoformat(), chat_id, chat_id),
            )
        return cursor.rowcount == 1

    def delete_cancelled_drafts_before(self, cutoff: datetime) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM drafts WHERE status = 'cancelled' AND cancelled_at < ?",
                (cutoff.astimezone(UTC).isoformat(),),
            )
        return cursor.rowcount

    def reopen_draft(self, chat_id: int, draft_id: int, version: int) -> InvoiceDraft:
        current = self.get_draft(chat_id, draft_id)
        if current.version != version or current.status != DraftStatus.PREVIEWED:
            raise StalePreviewError("Preview is no longer current")
        with self.connect() as connection:
            connection.execute(
                "UPDATE drafts SET status = 'collecting', preview_path = NULL, "
                "preview_digest = NULL WHERE id = ? AND chat_id = ?",
                (draft_id, chat_id),
            )
        return replace(
            current,
            status=DraftStatus.COLLECTING,
            preview_path=None,
            preview_digest=None,
        )

    def next_invoice_sequence(
        self,
        connection: sqlite3.Connection,
        chat_id: int,
    ) -> int:
        counter_name = f"invoice:{chat_id}"
        row = connection.execute(
            "SELECT value FROM counters WHERE name = ?",
            (counter_name,),
        ).fetchone()
        value = 1 if row is None else row["value"] + 1
        connection.execute(
            "INSERT INTO counters(name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (counter_name, value),
        )
        return value

    def serialize_draft(self, draft: InvoiceDraft) -> str:
        data = asdict(draft)
        data.pop("id")
        data.pop("version")
        data.pop("status")
        data.pop("preview_path")
        data.pop("preview_digest")
        data["issue_date"] = draft.issue_date.isoformat()
        if data["booking_fee"] is not None:
            data["booking_fee"] = str(data["booking_fee"])
        for item in data["items"]:
            item["unit_price"] = str(item["unit_price"])
        return json.dumps(data, ensure_ascii=False)

    def draft_from_row(self, row: sqlite3.Row) -> InvoiceDraft:
        data = json.loads(row["data"])
        items = tuple(
            InvoiceItem(
                description=item["description"],
                quantity=item["quantity"],
                unit_price=Decimal(item["unit_price"]),
                kind=item["kind"],
            )
            for item in data.pop("items")
        )
        data["issue_date"] = date.fromisoformat(data["issue_date"])
        if data.get("booking_fee") is not None:
            data["booking_fee"] = Decimal(data["booking_fee"])
        return InvoiceDraft(
            **data,
            items=items,
            id=row["id"],
            version=row["version"],
            status=DraftStatus(row["status"]),
            preview_path=row["preview_path"],
            preview_digest=row["preview_digest"],
        )

    def inbox_from_row(
        self,
        row: sqlite3.Row,
        status: str | None = None,
    ) -> InboxUpdate:
        return InboxUpdate(
            update_id=row["update_id"],
            payload=json.loads(row["payload"]),
            status=status or row["status"],
            error=row["error"],
        )
