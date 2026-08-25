from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from invoice_agent.config import Settings
from invoice_agent.domain import (
    InvoiceDraft,
    InvoiceItem,
    calculate_totals,
    validate_draft,
)


def make_complete_draft() -> InvoiceDraft:
    return InvoiceDraft(
        invoice_number="IV-2026-0001",
        issue_date=date(2026, 8, 12),
        customer_names="Yeoh Hong Shiong & Tan Li Yin",
        contact_number="+60149825136",
        event_date="29 November 2026",
        event_time="Luncheon",
        venue="Sutera Pekin, Johor",
        language="Mandarin (50%) & English (50%)",
        event_style="Western Style",
        table_count=45,
        items=[InvoiceItem("Professional Emcee Hosting", 1, Decimal("101.01"))],
    )


def test_required_fields_are_reported_in_conversation_order() -> None:
    draft = InvoiceDraft(
        invoice_number="IV-2026-0001",
        issue_date=date(2026, 8, 12),
    )

    validation = validate_draft(draft)

    assert validation.is_valid is False
    assert validation.missing_fields[0] == "customer_names"
    assert validation.missing_fields[-2:] == ("table_count", "primary_service")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_time", "Lunch"),
        ("event_style", "Western banquet"),
        ("language", "Mandarin and English"),
    ),
)
def test_invalid_choice_is_reported_as_missing(field: str, value: str) -> None:
    validation = validate_draft(replace(make_complete_draft(), **{field: value}))

    assert field in validation.missing_fields


def test_optional_items_do_not_block_validation() -> None:
    validation = validate_draft(make_complete_draft())

    assert validation.is_valid is True
    assert validation.missing_fields == ()


@pytest.mark.parametrize("table_count", [0, -1])
def test_table_count_must_be_positive(table_count: int) -> None:
    validation = validate_draft(replace(make_complete_draft(), table_count=table_count))

    assert validation.missing_fields == ("table_count",)


def test_pax_count_can_replace_table_count() -> None:
    draft = replace(make_complete_draft(), table_count=None, pax_count=300)

    validation = validate_draft(draft)

    assert validation.is_valid is True
    assert validation.missing_fields == ()


def test_totals_use_decimal_and_split_odd_cent() -> None:
    totals = calculate_totals([InvoiceItem("Hosting", 1, Decimal("101.01"))])

    assert totals.total == Decimal("101.01")
    assert totals.booking_fee == Decimal("50.51")
    assert totals.balance == Decimal("50.50")


def test_outstation_fee_is_excluded_from_all_totals() -> None:
    totals = calculate_totals(
        [
            InvoiceItem("Hosting", 1, Decimal("2000.00")),
            InvoiceItem("ROM", 1, Decimal("500.00"), "rom"),
            InvoiceItem(
                "KL Outstation Accommodation & Transportation",
                1,
                Decimal("400.00"),
                "outstation",
            ),
        ]
    )

    assert totals.total == Decimal("2500.00")
    assert totals.booking_fee == Decimal("1250.00")
    assert totals.balance == Decimal("1250.00")


def test_manual_booking_fee_cannot_use_outstation_fee() -> None:
    with pytest.raises(ValueError, match="cannot exceed total"):
        calculate_totals(
            [
                InvoiceItem("Hosting", 1, Decimal("1000.00")),
                InvoiceItem(
                    "KL Outstation Accommodation & Transportation",
                    1,
                    Decimal("400.00"),
                    "outstation",
                ),
            ],
            Decimal("1200.00"),
        )


def test_manual_booking_fee_keeps_total_and_updates_balance() -> None:
    totals = calculate_totals(
        [InvoiceItem("Hosting", 1, Decimal("2000.00"))],
        Decimal("800.00"),
    )

    assert totals.total == Decimal("2000.00")
    assert totals.booking_fee == Decimal("800.00")
    assert totals.balance == Decimal("1200.00")


def test_booking_fee_cannot_exceed_total() -> None:
    with pytest.raises(ValueError, match="cannot exceed total"):
        calculate_totals(
            [InvoiceItem("Hosting", 1, Decimal("1000.00"))],
            Decimal("1000.01"),
        )


def test_editing_a_draft_creates_an_independent_value() -> None:
    original = make_complete_draft()

    edited = replace(original, venue="New venue")

    assert original.venue == "Sutera Pekin, Johor"
    assert edited.venue == "New venue"


def test_settings_require_telegram_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "123")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.3-codex")

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()


def test_settings_parse_allowed_telegram_chat_ids() -> None:
    settings = Settings.from_env(
        {
            "TELEGRAM_BOT_TOKEN": "token",
            "TELEGRAM_ALLOWED_CHAT_IDS": " 123,456,123 ",
            "TELEGRAM_WEBHOOK_SECRET": "secret",
            "OPENAI_API_KEY": "key",
            "OPENAI_BASE_URL": "https://api.openai.com/v1",
            "OPENAI_MODEL": "gpt-5.3-codex",
        }
    )

    assert settings.telegram_allowed_chat_ids == (123, 456)


@pytest.mark.parametrize("value", ["", " , ", "123,nope"])
def test_settings_reject_invalid_telegram_chat_ids(value: str) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_ALLOWED_CHAT_IDS": value,
        "TELEGRAM_WEBHOOK_SECRET": "secret",
        "OPENAI_API_KEY": "key",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-5.3-codex",
    }

    with pytest.raises(ValueError, match="TELEGRAM_ALLOWED_CHAT_IDS"):
        Settings.from_env(env)
