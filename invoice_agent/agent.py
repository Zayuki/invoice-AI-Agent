import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from deepagents import (
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from invoice_agent.config import Settings
from invoice_agent.domain import (
    DraftStatus,
    EventStyle,
    InvoiceDraft,
    InvoiceItem,
    calculate_totals,
    is_valid_language,
    validate_draft,
)
from invoice_agent.rendering import PdfRenderer
from invoice_agent.store import Store

SYSTEM_PROMPT = """You create one wedding invoice for its owner.
Preserve customer names exactly, including Chinese names.
Translate all wedding reception details and service descriptions to English before saving them.
Never invent facts, selections, quantities, dates, or prices.
Use invoice tools for every draft read, update, validation, cancellation, and PDF preparation.
Call get_draft before changing a draft. Call validate_draft after every update.
When validation reports missing data, ask exactly one short clarifying question.
When validation succeeds, call prepare_pdf immediately.
Never ask the owner to send or type 'PDF'.
Keep Telegram replies brief and never use tables.
Optional services are outstation, ROM hosting, Floor Manager (fm), and Wedding DJ.
For explicit remove, cancel, or do-not-include requests, pass optional service kinds in remove_item_kinds.
When outstation service is selected, ask for its destination first and its fee second, one short question at a time.
Only create the outstation item after both are supplied. Use description `{Destination} Outstation Accommodation & Transportation`, quantity 1, and the owner-entered fee as unit_price.
Never default an outstation destination or fee.
Time must be exactly Dinner or Luncheon. Treat lunch as Luncheon.
Language must be Mandarin or English with percentages totalling 100%, formatted like Mandarin (100%), English (100%), or Mandarin (50%) & English (50%).
Event style must be exactly Oriental Style, Western Style, or Buffet Style.
Collect either table_count or pax_count as a positive whole number. Never add it to a service description.
When the owner corrects saved invoice data, use update_draft to replace it.
Never rename these service headings: Professional Emcee Hosting, ROM Hosting (Before Dinner Start), Program Planning & Management, Floor Manager, and Wedding DJ Services.
Never claim approval. Never send or address anything to a customer.
"""
FORBIDDEN_DEEP_TOOLS = {
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
TOOL_PROGRESS = {
    "get_draft": "🔍 Reading your invoice…",
    "update_draft": "✍️ Updating invoice details…",
    "validate_draft": "✅ Checking required fields…",
    "prepare_pdf": "📄 Generating PDF preview…",
    "discard_draft": "🗑️ Cancelling invoice…",
}


class ToolProgress(AsyncCallbackHandler):
    def __init__(
        self,
        update: Callable[[str], Awaitable[Any]],
        status: str,
    ) -> None:
        self.update = update
        self.status = status

    async def set_status(self, status: str) -> None:
        if status == self.status:
            return
        self.status = status
        with suppress(Exception):
            await self.update(status)

    async def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        status = TOOL_PROGRESS.get(serialized.get("name", ""))
        if status:
            await self.set_status(status)


# OpenAI rejects the lookahead in Pydantic's Decimal string pattern.
Money = Annotated[Decimal, WithJsonSchema({"type": "string"})]


class InvoiceItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    unit_price: Money = Field(ge=0)
    kind: Literal["primary", "outstation", "rom", "floor_manager", "dj"]

    @model_validator(mode="after")
    def has_outstation_destination(self) -> "InvoiceItemInput":
        suffix = " Outstation Accommodation & Transportation"
        if self.kind == "outstation" and not self.description.endswith(suffix):
            raise ValueError("outstation description must use the standard wording")
        if (
            self.kind == "outstation"
            and not self.description.removesuffix(suffix).strip()
        ):
            raise ValueError("outstation description must start with a destination")
        return self


OptionalItemKind = Literal["outstation", "rom", "floor_manager", "dj"]


class UpdateDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_number: str | None = Field(default=None, min_length=1)
    issue_date: date | None = None
    customer_names: str | None = None
    contact_number: str | None = None
    event_date: str | None = None
    event_time: Literal["Dinner", "Luncheon"] | None = None
    venue: str | None = None
    booking_fee: Money | None = Field(default=None, ge=0)
    items: list[InvoiceItemInput] | None = None
    remove_item_kinds: list[OptionalItemKind] = Field(default_factory=list)
    language: str | None = None
    event_style: EventStyle | None = None
    table_count: int | None = Field(default=None, gt=0)
    pax_count: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def has_one_event_size(self) -> "UpdateDraftInput":
        if self.table_count is not None and self.pax_count is not None:
            raise ValueError("provide table_count or pax_count, not both")
        return self

    @field_validator("language")
    @classmethod
    def language_has_valid_percentages(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_language(value):
            raise ValueError("language must use percentages totalling 100%")
        return value


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscardDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: bool


@dataclass(frozen=True)
class AgentReply:
    text: str
    pdf_path: Path | None = None
    draft_id: int | None = None
    version: int | None = None


class InvoiceTools:
    def __init__(
        self,
        store: Store,
        renderer: PdfRenderer,
        output_dir: Path,
        chat_id: int,
    ) -> None:
        self.store = store
        self.renderer = renderer
        self.output_dir = output_dir / str(chat_id)
        self.chat_id = chat_id

    def as_tools(self) -> list[BaseTool]:
        return [
            StructuredTool.from_function(
                func=self.get_draft,
                name="get_draft",
                description="Read the current structured invoice draft.",
                args_schema=EmptyInput,
            ),
            StructuredTool.from_function(
                func=self.update_draft,
                name="update_draft",
                description="Create, correct, or remove invoice facts explicitly supplied by the owner.",
                args_schema=UpdateDraftInput,
            ),
            StructuredTool.from_function(
                func=self.validate_current_draft,
                name="validate_draft",
                description="List required invoice fields that are still missing.",
                args_schema=EmptyInput,
            ),
            StructuredTool.from_function(
                coroutine=self.prepare_pdf,
                name="prepare_pdf",
                description="Render a PDF preview only after validation succeeds.",
                args_schema=EmptyInput,
            ),
            StructuredTool.from_function(
                func=self.discard_draft,
                name="discard_draft",
                description="Cancel the active draft only after explicit confirmation.",
                args_schema=DiscardDraftInput,
            ),
        ]

    def current_draft(self) -> InvoiceDraft:
        issue_date = datetime.now(ZoneInfo("Asia/Kuala_Lumpur")).date()
        return self.store.get_or_create_draft(self.chat_id, issue_date)

    def get_draft(self) -> str:
        return json.dumps(
            draft_for_agent(self.current_draft()),
            ensure_ascii=False,
        )

    def update_draft(self, **kwargs: Any) -> str:
        update = UpdateDraftInput.model_validate(kwargs)
        changes = update.model_dump(exclude_unset=True, exclude_none=True)
        remove_item_kinds = set(changes.pop("remove_item_kinds", ()))
        if "pax_count" in changes:
            changes["table_count"] = None
        elif "table_count" in changes:
            changes["pax_count"] = None
        if not changes and not remove_item_kinds:
            return self.get_draft()
        current = self.current_draft()
        existing_items = tuple(
            item for item in current.items if item.kind not in remove_item_kinds
        )
        if changes.get("items") is not None:
            incoming_items = tuple(
                InvoiceItem(
                    description=item.description,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    kind=item.kind,
                )
                for item in update.items or []
            )
            changes["items"] = merge_items(existing_items, incoming_items)
        elif remove_item_kinds:
            changes["items"] = existing_items
        candidate = replace(current, **changes)
        try:
            calculate_totals(candidate.items, candidate.booking_fee)
        except ValueError as error:
            return json.dumps({"status": "invalid", "error": str(error)})
        saved = self.store.save_draft(self.chat_id, candidate)
        return json.dumps(draft_for_agent(saved), ensure_ascii=False)

    def validate_current_draft(self) -> str:
        validation = validate_draft(self.current_draft())
        return json.dumps(asdict(validation), ensure_ascii=False)

    async def prepare_pdf(self) -> str:
        draft = self.current_draft()
        validation = validate_draft(draft)
        if not validation.is_valid:
            return json.dumps(
                {"status": "missing_fields", "fields": validation.missing_fields}
            )
        if draft.status == DraftStatus.PREVIEWED and draft.preview_path:
            return preview_result(draft)
        destination = self.output_dir / (f"{draft.invoice_number}-v{draft.version}.pdf")
        rendered = await self.renderer.render(draft, destination)
        previewed = self.store.save_preview(
            self.chat_id,
            draft.id,
            draft.version,
            rendered.path,
            rendered.digest,
        )
        return preview_result(previewed)

    def discard_draft(self, confirm: bool) -> str:
        if not confirm:
            return json.dumps({"status": "confirmation_required"})
        draft = self.current_draft()
        self.store.cancel_draft(self.chat_id, draft.id)
        return json.dumps({"status": "cancelled", "draft_id": draft.id})


class AgentService:
    def __init__(self, agent: Any, tools: InvoiceTools, thread_id: str) -> None:
        self.agent = agent
        self.tools = tools
        self.thread_id = thread_id

    async def reset(self) -> None:
        self.tools.store.cancel_active_draft(self.tools.chat_id)
        await self.agent.checkpointer.adelete_thread(self.thread_id)

    async def reply(
        self,
        text: str,
        progress: ToolProgress | None = None,
    ) -> AgentReply:
        config: dict[str, Any] = {"configurable": {"thread_id": self.thread_id}}
        if progress:
            config["callbacks"] = [progress]
        result = await self.agent.ainvoke(
            {"messages": [HumanMessage(content=text)]},
            config=config,
        )
        response_text = message_content(result["messages"][-1].content)
        if not response_text.strip():
            raise RuntimeError("Model returned an empty response")
        draft = self.tools.current_draft()
        if draft.status == DraftStatus.PREVIEWED and draft.preview_path:
            return AgentReply(
                text=response_text,
                pdf_path=Path(draft.preview_path),
                draft_id=draft.id,
                version=draft.version,
            )
        return AgentReply(text=response_text)


def draft_for_agent(draft: InvoiceDraft) -> dict[str, Any]:
    return {
        "draft_id": draft.id,
        "version": draft.version,
        "status": draft.status,
        "invoice_number": draft.invoice_number,
        "issue_date": draft.issue_date.isoformat(),
        "customer_names": draft.customer_names,
        "contact_number": draft.contact_number,
        "event_date": draft.event_date,
        "event_time": draft.event_time,
        "venue": draft.venue,
        "booking_fee": str(draft.booking_fee)
        if draft.booking_fee is not None
        else None,
        "items": [
            {
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "kind": item.kind,
            }
            for item in draft.items
        ],
        "language": draft.language,
        "event_style": draft.event_style,
        "table_count": draft.table_count,
        "pax_count": draft.pax_count,
    }


def merge_items(
    existing: Sequence[InvoiceItem],
    incoming: tuple[InvoiceItem, ...],
) -> tuple[InvoiceItem, ...]:
    replacements = {item.kind: item for item in incoming}
    merged = [replacements.pop(item.kind, item) for item in existing]
    merged.extend(replacements.values())
    return tuple(merged)


def preview_result(draft: InvoiceDraft) -> str:
    return json.dumps(
        {
            "status": "preview_ready",
            "draft_id": draft.id,
            "version": draft.version,
            "path": draft.preview_path,
        }
    )


def message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content)


def register_invoice_profile(model_name: str) -> None:
    register_harness_profile(
        f"openai:{model_name}",
        HarnessProfile(
            excluded_tools=frozenset(FORBIDDEN_DEEP_TOOLS),
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )


def build_model(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        max_completion_tokens=16_384,
        reasoning_effort="medium",
        use_responses_api=True,
    )


def build_agent(
    settings: Settings,
    invoice_tools: InvoiceTools,
    checkpointer: Any,
) -> Any:
    register_invoice_profile(settings.openai_model)
    return create_deep_agent(
        model=build_model(settings),
        tools=invoice_tools.as_tools(),
        system_prompt=SYSTEM_PROMPT,
        subagents=[],
        permissions=[
            FilesystemPermission(
                operations=["read", "write"],
                paths=["/**"],
                mode="deny",
            )
        ],
        checkpointer=checkpointer,
    )
