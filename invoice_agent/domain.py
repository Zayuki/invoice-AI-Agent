import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Literal, get_args

EventStyle = Literal["Oriental Style", "Western Style", "Buffet Style"]

ItemKind = Literal["primary", "outstation", "rom", "floor_manager", "dj"]
OptionalItemKind = Literal["outstation", "rom", "floor_manager", "dj"]

SERVICE_HEADINGS: dict[str, str] = {
    "primary": "Professional Emcee Hosting",
    "rom": "ROM Hosting (Before Dinner Start)",
    "floor_manager": "Floor Manager",
    "dj": "Wedding DJ Services",
}

FIXED_HEADINGS: tuple[str, ...] = (
    SERVICE_HEADINGS["primary"],
    SERVICE_HEADINGS["rom"],
    "Program Planning & Management",
    SERVICE_HEADINGS["floor_manager"],
    SERVICE_HEADINGS["dj"],
)


class DraftStatus(StrEnum):
    COLLECTING = "collecting"
    PREVIEWED = "previewed"
    APPROVED = "approved"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class InvoiceItem:
    description: str
    quantity: int
    unit_price: Decimal
    kind: str = "primary"

    @property
    def amount(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass(frozen=True)
class InvoiceDraft:
    invoice_number: str
    issue_date: date
    customer_names: str | None = None
    contact_number: str | None = None
    event_date: str | None = None
    event_time: str | None = None
    venue: str | None = None
    language: str | None = None
    event_style: EventStyle | None = None
    table_count: int | None = None
    pax_count: int | None = None
    booking_fee: Decimal | None = None
    items: Sequence[InvoiceItem] = field(default_factory=tuple)
    id: int | None = None
    version: int = 0
    status: DraftStatus = DraftStatus.COLLECTING
    preview_path: str | None = None
    preview_digest: str | None = None


@dataclass(frozen=True)
class DraftValidation:
    is_valid: bool
    missing_fields: tuple[str, ...]


@dataclass(frozen=True)
class InvoiceTotals:
    total: Decimal
    booking_fee: Decimal
    balance: Decimal


REQUIRED_FIELDS = (
    "customer_names",
    "contact_number",
    "event_date",
    "event_time",
    "venue",
    "language",
    "event_style",
)

LANGUAGE_PATTERN = re.compile(
    r"^(?:Mandarin \(100%\)|English \(100%\)|"
    r"Mandarin \((\d{1,3})%\) & English \((\d{1,3})%\))$"
)


def is_valid_language(value: str) -> bool:
    match = LANGUAGE_PATTERN.fullmatch(value)
    if match is None:
        return False
    percentages = match.groups()
    return percentages == (None, None) or sum(map(int, percentages)) == 100


def validate_draft(draft: InvoiceDraft) -> DraftValidation:
    missing = [name for name in REQUIRED_FIELDS if not getattr(draft, name)]
    if not any(
        count is not None and count > 0
        for count in (draft.table_count, draft.pax_count)
    ):
        missing.append("table_count")
    if draft.event_time and draft.event_time not in {"Dinner", "Luncheon"}:
        missing.append("event_time")
    if draft.event_style and draft.event_style not in get_args(EventStyle):
        missing.append("event_style")
    if draft.language and not is_valid_language(draft.language):
        missing.append("language")
    primary_items = [item for item in draft.items if item.kind == "primary"]
    if not primary_items:
        missing.append("primary_service")
    return DraftValidation(not missing, tuple(missing))


def calculate_totals(
    items: Sequence[InvoiceItem],
    booking_fee: Decimal | None = None,
) -> InvoiceTotals:
    total = sum(
        (item.amount for item in items if item.kind != "outstation"),
        Decimal("0.00"),
    )
    total = total.quantize(Decimal("0.01"), ROUND_HALF_UP)
    fee = booking_fee if booking_fee is not None else total / 2
    fee = fee.quantize(Decimal("0.01"), ROUND_HALF_UP)
    if fee > total:
        raise ValueError("Booking fee cannot exceed total amount")
    return InvoiceTotals(total, fee, total - fee)
