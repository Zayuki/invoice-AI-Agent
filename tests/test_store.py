import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any, Self

import pytest

from invoice_agent.domain import DraftStatus, InvoiceDraft, InvoiceItem
from invoice_agent.store import StalePreviewError, Store


class ApproveBeforeCancelConnection:
    def __init__(
        self,
        connection: Any,
        database_path: Path,
        draft_id: int,
    ) -> None:
        self.connection = connection
        self.database_path = database_path
        self.draft_id = draft_id

    def __enter__(self) -> Self:
        self.connection.__enter__()
        return self

    def __exit__(self, *args: object) -> Any:
        return self.connection.__exit__(*args)

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        if sql.startswith("UPDATE drafts SET status = 'cancelled'"):
            with Store(self.database_path).connect() as competing:
                competing.execute(
                    "UPDATE drafts SET status = 'approved' WHERE id = ?",
                    (self.draft_id,),
                )
        return self.connection.execute(sql, parameters)


class ApproveBeforeCancelFactory:
    def __init__(self, store: Store, draft_id: int) -> None:
        self.original_connect = store.connect
        self.database_path = store.path
        self.draft_id = draft_id

    def __call__(self) -> ApproveBeforeCancelConnection:
        return ApproveBeforeCancelConnection(
            self.original_connect(),
            self.database_path,
            self.draft_id,
        )


def make_draft() -> InvoiceDraft:
    return InvoiceDraft(
        invoice_number="IV-2026-0001",
        issue_date=date(2026, 8, 12),
        customer_names="Yeoh Hong Shiong & Tan Li Yin",
        contact_number="+60149825136",
        event_date="29 November 2026",
        event_time="Luncheon",
        venue="Sutera Pekin, Johor",
        language="Mandarin (50%) & English (50%)",
        event_style="Western",
        table_count=45,
        items=(InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),),
    )


@pytest.fixture
def store(tmp_path) -> Store:
    result = Store(tmp_path / "invoice.db")
    result.initialize(123)
    return result


def test_duplicate_update_is_inserted_once(store: Store) -> None:
    assert store.enqueue_update(42, {"update_id": 42}) is True
    assert store.enqueue_update(42, {"update_id": 42}) is False


def test_processing_update_is_recovered_after_restart(tmp_path) -> None:
    database_path = tmp_path / "invoice.db"
    first = Store(database_path)
    first.initialize(123)
    first.enqueue_update(7, {"update_id": 7})
    assert first.claim_next_update().update_id == 7

    second = Store(database_path)
    second.initialize(123)

    assert second.claim_next_update().update_id == 7


def test_completed_update_is_not_claimed_again(store: Store) -> None:
    store.enqueue_update(9, {"update_id": 9})
    claimed = store.claim_next_update()
    store.complete_update(claimed.update_id)

    assert store.claim_next_update() is None


def test_chats_have_independent_drafts_and_invoice_sequences(store: Store) -> None:
    first = store.create_draft(123, date(2026, 8, 12))
    second = store.create_draft(456, date(2026, 8, 12))

    assert first.invoice_number == "IV-2026-0001"
    assert second.invoice_number == "IV-2026-0001"
    assert store.get_or_create_draft(123, date(2026, 8, 12)).id == first.id
    assert store.get_or_create_draft(456, date(2026, 8, 12)).id == second.id


def test_existing_draft_without_table_count_still_loads(store: Store) -> None:
    draft = store.insert_draft(123, make_draft())
    with store.connect() as connection:
        row = connection.execute(
            "SELECT data FROM drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()
        data = json.loads(row["data"])
        data.pop("table_count")
        connection.execute(
            "UPDATE drafts SET data = ? WHERE id = ?",
            (json.dumps(data), draft.id),
        )

    assert store.get_draft(123, draft.id).table_count is None


def test_chat_cannot_read_another_chats_draft(store: Store) -> None:
    draft = store.create_draft(123, date(2026, 8, 12))

    with pytest.raises(KeyError):
        store.get_draft(456, draft.id)


def test_initialize_migrates_existing_owner_data(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    legacy_store = Store(database_path)
    draft = make_draft()
    with legacy_store.connect() as connection:
        connection.executescript(
            """
            CREATE TABLE inbox (
                update_id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                issue_date TEXT NOT NULL,
                data TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                preview_path TEXT,
                preview_digest TEXT
            );
            CREATE TABLE counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO drafts(invoice_number, issue_date, data, version, status) "
            "VALUES (?, ?, ?, 0, ?)",
            (
                draft.invoice_number,
                draft.issue_date.isoformat(),
                legacy_store.serialize_draft(draft),
                DraftStatus.COLLECTING,
            ),
        )
        connection.execute("INSERT INTO counters VALUES ('invoice', 4)")

    legacy_store.initialize(123)

    with legacy_store.connect() as connection:
        columns = {
            column["name"]
            for column in connection.execute("PRAGMA table_info(drafts)").fetchall()
        }

    assert "cancelled_at" in columns
    assert legacy_store.get_draft(123, 1).invoice_number == "IV-2026-0001"
    assert legacy_store.create_draft(123, date(2026, 8, 12)).invoice_number == (
        "IV-2026-0005"
    )
    assert legacy_store.create_draft(456, date(2026, 8, 12)).invoice_number == (
        "IV-2026-0001"
    )


def test_edit_invalidates_previous_preview(store: Store, tmp_path) -> None:
    saved = store.save_draft(123, make_draft())
    preview_path = tmp_path / "v1.pdf"
    preview_path.write_bytes(b"pdf")
    previewed = store.save_preview(
        123,
        saved.id,
        saved.version,
        preview_path,
        sha256(preview_path.read_bytes()).hexdigest(),
    )

    edited = store.save_draft(123, replace(previewed, venue="New venue"))

    assert edited.version == previewed.version + 1
    assert edited.status == DraftStatus.COLLECTING
    with pytest.raises(StalePreviewError):
        store.approve_preview(123, edited.id, previewed.version)


def test_booking_fee_round_trips_as_decimal(store: Store) -> None:
    saved = store.save_draft(123, replace(make_draft(), booking_fee=Decimal("800.00")))

    assert store.get_draft(123, saved.id).booking_fee == Decimal("800.00")


def test_approval_returns_exact_current_preview(store: Store, tmp_path) -> None:
    saved = store.save_draft(123, make_draft())
    preview_path = tmp_path / "invoice.pdf"
    preview_path.write_bytes(b"exact reviewed bytes")
    previewed = store.save_preview(
        123,
        saved.id,
        saved.version,
        preview_path,
        sha256(preview_path.read_bytes()).hexdigest(),
    )

    approved_path = store.approve_preview(123, previewed.id, previewed.version)

    assert approved_path.read_bytes() == b"exact reviewed bytes"
    assert store.get_draft(123, previewed.id).status == DraftStatus.APPROVED


def test_approval_rejects_preview_whose_bytes_changed(
    store: Store,
    tmp_path,
) -> None:
    saved = store.save_draft(123, make_draft())
    preview_path = tmp_path / "invoice.pdf"
    preview_path.write_bytes(b"reviewed")
    previewed = store.save_preview(
        123,
        saved.id,
        saved.version,
        preview_path,
        sha256(preview_path.read_bytes()).hexdigest(),
    )
    preview_path.write_bytes(b"changed")

    with pytest.raises(StalePreviewError, match="bytes"):
        store.approve_preview(123, previewed.id, previewed.version)


def test_cancel_rejects_stale_preview_version(store: Store, tmp_path) -> None:
    saved = store.save_draft(123, make_draft())
    preview_path = tmp_path / "invoice.pdf"
    preview_path.write_bytes(b"pdf")
    previewed = store.save_preview(
        123,
        saved.id,
        saved.version,
        preview_path,
        "digest",
    )
    edited = store.reopen_draft(123, previewed.id, previewed.version)
    edited = store.save_draft(123, replace(edited, venue="New venue"))

    with pytest.raises(StalePreviewError):
        store.cancel_draft(123, edited.id, previewed.version)


def test_cancelled_invoice_number_is_not_reused(store: Store) -> None:
    first = store.create_draft(123, date(2026, 8, 12))
    store.cancel_draft(123, first.id)

    second = store.create_draft(123, date(2026, 8, 12))

    assert first.invoice_number == "IV-2026-0001"
    assert second.invoice_number == "IV-2026-0002"


def test_only_expired_cancelled_drafts_are_deleted(store: Store) -> None:
    old = store.create_draft(123, date(2026, 8, 12))
    store.cancel_draft(123, old.id)
    recent = store.create_draft(123, date(2026, 8, 12))
    store.cancel_draft(123, recent.id)
    active = store.create_draft(123, date(2026, 8, 12))
    now = datetime.now(UTC)
    with store.connect() as connection:
        connection.execute(
            "UPDATE drafts SET cancelled_at = ? WHERE id = ?",
            ((now - timedelta(days=31)).isoformat(), old.id),
        )
        connection.execute(
            "UPDATE drafts SET cancelled_at = ? WHERE id = ?",
            ((now - timedelta(days=29)).isoformat(), recent.id),
        )

    deleted = store.delete_cancelled_drafts_before(now - timedelta(days=30))

    assert deleted == 1
    with pytest.raises(KeyError):
        store.get_draft(123, old.id)
    assert store.get_draft(123, recent.id).status == DraftStatus.CANCELLED
    assert store.get_draft(123, active.id).status == DraftStatus.COLLECTING


def test_cancel_active_draft_preserves_history_and_numbering(
    store: Store,
    tmp_path,
) -> None:
    assert store.cancel_active_draft(123) is False

    collecting = store.create_draft(123, date(2026, 8, 12))
    assert store.cancel_active_draft(123) is True
    assert store.get_draft(123, collecting.id).status == DraftStatus.CANCELLED
    with store.connect() as connection:
        cancelled_at = connection.execute(
            "SELECT cancelled_at FROM drafts WHERE id = ?",
            (collecting.id,),
        ).fetchone()["cancelled_at"]
    assert cancelled_at is not None

    approved = store.create_draft(123, date(2026, 8, 12))
    approved_path = tmp_path / "approved.pdf"
    approved_path.write_bytes(b"approved")
    previewed = store.save_preview(
        123,
        approved.id,
        approved.version,
        approved_path,
        sha256(approved_path.read_bytes()).hexdigest(),
    )
    store.approve_preview(123, previewed.id, previewed.version)

    active = store.create_draft(123, date(2026, 8, 12))
    active_path = tmp_path / "active.pdf"
    active_path.write_bytes(b"active")
    store.save_preview(
        123,
        active.id,
        active.version,
        active_path,
        sha256(active_path.read_bytes()).hexdigest(),
    )

    assert store.cancel_active_draft(123) is True
    assert store.get_draft(123, active.id).status == DraftStatus.CANCELLED
    assert store.get_draft(123, approved.id).status == DraftStatus.APPROVED
    assert store.create_draft(123, date(2026, 8, 12)).invoice_number == "IV-2026-0004"


def test_cancel_active_draft_does_not_overwrite_approval(
    store: Store,
    monkeypatch,
) -> None:
    draft = store.create_draft(123, date(2026, 8, 12))
    monkeypatch.setattr(store, "connect", ApproveBeforeCancelFactory(store, draft.id))

    assert store.cancel_active_draft(123) is False
    assert store.get_draft(123, draft.id).status == DraftStatus.APPROVED


def test_connection_closes_after_with_block(store: Store) -> None:
    with store.connect() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")


def test_database_runs_in_wal_mode(store: Store) -> None:
    with store.connect() as connection:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

    assert mode == "wal"
