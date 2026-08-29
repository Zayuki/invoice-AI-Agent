import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import get_args
from unittest.mock import AsyncMock, call

import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError

from invoice_agent.agent import (
    FORBIDDEN_DEEP_TOOLS,
    SYSTEM_PROMPT,
    AgentService,
    InvoiceItemInput,
    InvoiceTools,
    ToolProgress,
    UpdateDraftInput,
    build_agent,
    build_model,
)
from invoice_agent.config import Settings
from invoice_agent.domain import (
    EventStyle,
    FIXED_HEADINGS,
    OptionalItemKind,
    SERVICE_HEADINGS,
)
from invoice_agent.rendering import PdfRenderer
from invoice_agent.store import Store


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        telegram_bot_token="token",
        telegram_allowed_chat_ids=(123,),
        telegram_webhook_secret="secret",
        openai_api_key="key",
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-5.3-codex",
        database_path=tmp_path / "invoice.db",
        output_dir=tmp_path / "generated",
    )


@pytest.fixture
def invoice_tools(settings: Settings) -> InvoiceTools:
    store = Store(settings.database_path)
    store.initialize(123)
    store.create_draft(123, date(2026, 8, 12))
    return InvoiceTools(store, PdfRenderer(), settings.output_dir, 123)


def test_agent_exposes_only_invoice_application_tools(
    invoice_tools: InvoiceTools,
) -> None:
    assert {tool.name for tool in invoice_tools.as_tools()} == {
        "get_draft",
        "update_draft",
        "validate_draft",
        "prepare_pdf",
        "discard_draft",
    }


@pytest.mark.asyncio
async def test_tool_progress_reports_real_agent_stage(
    invoice_tools: InvoiceTools,
) -> None:
    update = AsyncMock()
    progress = ToolProgress(update, "🔍 Reading your invoice…")
    tools = {tool.name: tool for tool in invoice_tools.as_tools()}
    config = {"callbacks": [progress]}

    await tools["update_draft"].ainvoke(
        {"customer_names": "ALICE & BOB"},
        config=config,
    )
    await tools["validate_draft"].ainvoke({}, config=config)
    await tools["validate_draft"].ainvoke({}, config=config)

    assert update.await_args_list == [
        call("✍️ Updating invoice details…"),
        call("✅ Checking required fields…"),
    ]


def test_update_schema_rejects_calculated_totals() -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"total": "999.00"})


def test_outstation_item_accepts_owner_destination_and_fee() -> None:
    item = InvoiceItemInput.model_validate(
        {
            "description": "Melaka Outstation Accommodation & Transportation",
            "quantity": 1,
            "unit_price": "300.00",
            "kind": "outstation",
        }
    )

    assert item.description == "Melaka Outstation Accommodation & Transportation"
    assert item.unit_price == Decimal("300.00")


@pytest.mark.parametrize(
    "description",
    (
        "Outstation Accommodation & Transportation",
        "KL Outstation Accommodation",
        "KL Outstation Accomodation & Transportation",
    ),
)
def test_outstation_item_requires_destination_and_standard_wording(
    description: str,
) -> None:
    with pytest.raises(ValidationError):
        InvoiceItemInput.model_validate(
            {
                "description": description,
                "quantity": 1,
                "unit_price": "400.00",
                "kind": "outstation",
            }
        )


@pytest.mark.parametrize("event_time", ["Lunch", "Breakfast", "luncheon"])
def test_update_schema_only_accepts_dinner_or_luncheon(event_time: str) -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"event_time": event_time})


@pytest.mark.parametrize(
    "event_style", ["Oriental Style", "Western Style", "Buffet Style"]
)
def test_update_schema_accepts_event_styles(event_style: str) -> None:
    update = UpdateDraftInput.model_validate({"event_style": event_style})

    assert update.event_style == event_style


def test_update_schema_rejects_invalid_event_style() -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"event_style": "Western banquet"})


@pytest.mark.parametrize("table_count", [30, 45])
def test_update_schema_accepts_positive_table_count(table_count: int) -> None:
    update = UpdateDraftInput.model_validate({"table_count": table_count})

    assert update.table_count == table_count


@pytest.mark.parametrize("table_count", [0, -1, 2.5])
def test_update_schema_rejects_invalid_table_count(table_count: float) -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"table_count": table_count})


@pytest.mark.parametrize("pax_count", [80, 100])
def test_update_schema_accepts_positive_pax_count(pax_count: int) -> None:
    update = UpdateDraftInput.model_validate({"pax_count": pax_count})

    assert update.pax_count == pax_count


@pytest.mark.parametrize("pax_count", [0, -1, 2.5])
def test_update_schema_rejects_invalid_pax_count(pax_count: float) -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"pax_count": pax_count})


def test_pax_count_replaces_existing_table_count(
    invoice_tools: InvoiceTools,
) -> None:
    invoice_tools.update_draft(table_count=30)

    invoice_tools.update_draft(pax_count=300)

    draft = invoice_tools.current_draft()
    assert draft.table_count is None
    assert draft.pax_count == 300


def test_table_count_replaces_existing_pax_count(
    invoice_tools: InvoiceTools,
) -> None:
    invoice_tools.update_draft(pax_count=300)

    invoice_tools.update_draft(table_count=30)

    draft = invoice_tools.current_draft()
    assert draft.table_count == 30
    assert draft.pax_count is None


def test_update_schema_rejects_table_and_pax_count_together() -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"table_count": 30, "pax_count": 300})


@pytest.mark.parametrize(
    "language",
    [
        "Mandarin (100%)",
        "English (100%)",
        "Mandarin (70%) & English (30%)",
    ],
)
def test_update_schema_accepts_language_percentages(language: str) -> None:
    update = UpdateDraftInput.model_validate({"language": language})

    assert update.language == language


@pytest.mark.parametrize(
    "language",
    ["Mandarin", "Mandarin and English", "Mandarin (70%) & English (40%)"],
)
def test_update_schema_rejects_invalid_language(language: str) -> None:
    with pytest.raises(ValidationError):
        UpdateDraftInput.model_validate({"language": language})


def test_agent_can_update_booking_fee(invoice_tools: InvoiceTools) -> None:
    invoice_tools.update_draft(
        items=[
            {
                "description": "Professional Emcee Hosting",
                "quantity": 1,
                "unit_price": "2000.00",
                "kind": "primary",
            }
        ]
    )

    result = json.loads(invoice_tools.update_draft(booking_fee="800.00"))

    assert invoice_tools.current_draft().booking_fee == Decimal("800.00")
    assert result["booking_fee"] == "800.00"


def test_agent_rejects_booking_fee_above_total(invoice_tools: InvoiceTools) -> None:
    invoice_tools.update_draft(
        items=[
            {
                "description": "Professional Emcee Hosting",
                "quantity": 1,
                "unit_price": "1000.00",
                "kind": "primary",
            }
        ]
    )

    result = json.loads(invoice_tools.update_draft(booking_fee="1000.01"))

    assert result["status"] == "invalid"
    assert invoice_tools.current_draft().booking_fee is None


def test_invoice_number_can_be_updated(invoice_tools: InvoiceTools) -> None:
    invoice_tools.update_draft(invoice_number="IV-2026-0002")

    draft = invoice_tools.current_draft()
    with invoice_tools.store.connect() as connection:
        stored_number = connection.execute(
            "SELECT invoice_number FROM drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()["invoice_number"]
    invoice_tools.store.cancel_active_draft(123)
    next_draft = invoice_tools.store.create_draft(123, date(2026, 8, 12))

    assert draft.invoice_number == "IV-2026-0002"
    assert stored_number == "IV-2026-0002"
    assert next_draft.invoice_number == "IV-2026-0003"


def test_issue_date_can_be_updated_without_renumbering(
    invoice_tools: InvoiceTools,
) -> None:
    original = invoice_tools.current_draft()
    invoice_tools.store.save_preview(
        123,
        original.id,
        original.version,
        Path("preview.pdf"),
        "digest",
    )

    result = json.loads(invoice_tools.update_draft(issue_date="2027-01-02"))
    draft = invoice_tools.current_draft()
    with invoice_tools.store.connect() as connection:
        stored_date = connection.execute(
            "SELECT issue_date FROM drafts WHERE id = ?",
            (draft.id,),
        ).fetchone()["issue_date"]

    assert result["issue_date"] == "2027-01-02"
    assert draft.issue_date == date(2027, 1, 2)
    assert draft.invoice_number == original.invoice_number
    assert draft.preview_path is None
    assert stored_date == "2027-01-02"


def test_later_optional_service_preserves_primary_service(
    invoice_tools: InvoiceTools,
) -> None:
    invoice_tools.update_draft(
        items=[
            {
                "description": "Professional Emcee Hosting",
                "quantity": 1,
                "unit_price": "2188",
                "kind": "primary",
            }
        ]
    )

    invoice_tools.update_draft(
        items=[
            {
                "description": "Floor Manager",
                "quantity": 1,
                "unit_price": "120",
                "kind": "floor_manager",
            }
        ]
    )

    assert [item.kind for item in invoice_tools.current_draft().items] == [
        "primary",
        "floor_manager",
    ]


@pytest.mark.parametrize(
    ("removed_kind", "expected_kinds"),
    (
        ("outstation", ["primary", "rom", "floor_manager", "dj"]),
        ("rom", ["primary", "outstation", "floor_manager", "dj"]),
        ("floor_manager", ["primary", "outstation", "rom", "dj"]),
        ("dj", ["primary", "outstation", "rom", "floor_manager"]),
    ),
)
def test_optional_service_can_be_removed(
    invoice_tools: InvoiceTools,
    removed_kind: str,
    expected_kinds: list[str],
) -> None:
    invoice_tools.update_draft(
        items=[
            {
                "description": "Professional Emcee Hosting",
                "quantity": 1,
                "unit_price": "1888",
                "kind": "primary",
            },
            {
                "description": "KL Outstation Accommodation & Transportation",
                "quantity": 1,
                "unit_price": "300",
                "kind": "outstation",
            },
            {
                "description": "ROM Hosting",
                "quantity": 1,
                "unit_price": "488",
                "kind": "rom",
            },
            {
                "description": "Floor Manager",
                "quantity": 1,
                "unit_price": "120",
                "kind": "floor_manager",
            },
            {
                "description": "Wedding DJ",
                "quantity": 1,
                "unit_price": "300",
                "kind": "dj",
            },
        ]
    )

    invoice_tools.update_draft(remove_item_kinds=[removed_kind])

    assert [item.kind for item in invoice_tools.current_draft().items] == expected_kinds


def test_primary_service_cannot_be_removed() -> None:
    with pytest.raises(ValidationError) as error:
        UpdateDraftInput.model_validate({"remove_item_kinds": ["primary"]})

    assert error.value.errors()[0]["type"] == "literal_error"


def test_removed_optional_service_can_be_re_added(
    invoice_tools: InvoiceTools,
) -> None:
    invoice_tools.update_draft(
        items=[
            {
                "description": "Professional Emcee Hosting",
                "quantity": 1,
                "unit_price": "1888",
                "kind": "primary",
            },
            {
                "description": "Floor Manager",
                "quantity": 1,
                "unit_price": "120",
                "kind": "floor_manager",
            },
        ]
    )
    invoice_tools.update_draft(remove_item_kinds=["floor_manager"])

    invoice_tools.update_draft(
        items=[
            {
                "description": "Floor Manager",
                "quantity": 1,
                "unit_price": "150",
                "kind": "floor_manager",
            }
        ]
    )

    floor_manager = invoice_tools.current_draft().items[-1]
    assert floor_manager.kind == "floor_manager"
    assert floor_manager.unit_price == Decimal(150)


def test_incoming_optional_service_wins_over_removal(
    invoice_tools: InvoiceTools,
) -> None:
    invoice_tools.update_draft(
        remove_item_kinds=["floor_manager"],
        items=[
            {
                "description": "Floor Manager",
                "quantity": 1,
                "unit_price": "150",
                "kind": "floor_manager",
            }
        ],
    )

    floor_manager = invoice_tools.current_draft().items[0]
    assert floor_manager.kind == "floor_manager"
    assert floor_manager.unit_price == Decimal(150)


def test_null_update_values_preserve_existing_draft(
    invoice_tools: InvoiceTools,
) -> None:
    invoice_tools.update_draft(
        customer_names="Existing Couple",
        items=[
            {
                "description": "Professional Emcee Hosting",
                "quantity": 1,
                "unit_price": "2188",
                "kind": "primary",
            }
        ],
    )

    invoice_tools.update_draft(customer_names=None, items=None)

    draft = invoice_tools.current_draft()
    assert draft.customer_names == "Existing Couple"
    assert [item.kind for item in draft.items] == ["primary"]


def test_system_prompt_defines_invoice_flow() -> None:
    assert "one short clarifying question" in SYSTEM_PROMPT
    assert "Never invent" in SYSTEM_PROMPT
    assert "Translate all wedding reception details" in SYSTEM_PROMPT
    assert "Preserve customer names exactly" in SYSTEM_PROMPT
    assert "call prepare_pdf immediately" in SYSTEM_PROMPT
    assert "Never ask the owner to send or type 'PDF'" in SYSTEM_PROMPT
    assert "remove_item_kinds" in SYSTEM_PROMPT
    assert "Oriental Style, Western Style, or Buffet Style" in SYSTEM_PROMPT
    assert "Never rename these service headings" in SYSTEM_PROMPT
    assert "ask for its destination first and its fee second" in SYSTEM_PROMPT
    assert "Never default an outstation destination or fee" in SYSTEM_PROMPT


def test_system_prompt_uses_domain_constants() -> None:
    for style in get_args(EventStyle):
        assert style in SYSTEM_PROMPT
    for kind in get_args(OptionalItemKind):
        assert kind in SYSTEM_PROMPT
    for heading in FIXED_HEADINGS:
        assert heading in SYSTEM_PROMPT
    assert "{Destination} Outstation Accommodation & Transportation" in SYSTEM_PROMPT


def test_renderer_uses_service_headings() -> None:
    values = SERVICE_HEADINGS.values()
    assert "Professional Emcee Hosting" in values
    assert "ROM Hosting (Before Dinner Start)" in values
    assert "Floor Manager" in values
    assert "Wedding DJ Services" in values


def test_deep_agent_profile_excludes_general_tools() -> None:
    assert FORBIDDEN_DEEP_TOOLS == {
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "delete",
        "glob",
        "grep",
        "execute",
        "task",
        "write_todos",
    }


def test_model_uses_configured_codex_endpoint(settings: Settings) -> None:
    model = build_model(settings)

    assert model.model_name == "gpt-5.3-codex"
    assert str(model.openai_api_base) == "https://api.openai.com/v1"
    assert model.max_tokens == 16_384
    assert model.use_responses_api is True


@pytest.mark.asyncio
async def test_agent_rejects_empty_model_response(
    invoice_tools: InvoiceTools,
) -> None:
    graph = AsyncMock()
    graph.ainvoke.return_value = {"messages": [AIMessage(content=[])]}
    agent = AgentService(graph, invoice_tools, "123")

    with pytest.raises(RuntimeError, match="Model returned an empty response"):
        await agent.reply("Invoice details")


@pytest.mark.asyncio
async def test_clarifying_question_does_not_resend_earlier_preview(
    invoice_tools: InvoiceTools,
) -> None:
    draft = invoice_tools.current_draft()
    invoice_tools.store.save_preview(
        123,
        draft.id,
        draft.version,
        Path("preview.pdf"),
        "digest",
    )
    graph = AsyncMock()
    graph.ainvoke.return_value = {
        "messages": [AIMessage(content="Which outstation destination?")]
    }
    agent = AgentService(graph, invoice_tools, "123")

    reply = await agent.reply("Add outstation")

    assert reply.pdf_path is None


@pytest.mark.asyncio
async def test_prepared_preview_is_sent_with_the_reply(
    invoice_tools: InvoiceTools,
) -> None:
    draft = invoice_tools.current_draft()
    graph = AsyncMock()

    async def prepare_then_answer(*args, **kwargs):
        invoice_tools.store.save_preview(
            123,
            draft.id,
            draft.version,
            Path("preview.pdf"),
            "digest",
        )
        invoice_tools.prepared_preview = True
        return {"messages": [AIMessage(content="Invoice ready.")]}

    graph.ainvoke.side_effect = prepare_then_answer
    agent = AgentService(graph, invoice_tools, "123")

    reply = await agent.reply("Confirm")

    assert reply.pdf_path == Path("preview.pdf")


def test_build_agent_returns_compiled_graph(
    settings: Settings,
    invoice_tools: InvoiceTools,
) -> None:
    agent = build_agent(settings, invoice_tools, MemorySaver())

    assert {"__start__", "model", "tools"}.issubset(agent.nodes)
    assert Path(settings.output_dir).name == "generated"


class HangingGraph:
    async def ainvoke(self, *args, **kwargs):
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_reply_times_out_when_model_hangs(tmp_path):
    store = Store(tmp_path / "invoice.db")
    store.initialize(123)
    tools = InvoiceTools(store, PdfRenderer(), tmp_path / "generated", 123)
    service = AgentService(HangingGraph(), tools, "123", timeout=0.05)

    with pytest.raises(TimeoutError):
        await service.reply("Create invoice")
