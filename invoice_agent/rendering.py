import re
from dataclasses import dataclass
from hashlib import sha256
from html import escape
from pathlib import Path
from time import strftime, strptime

from jinja2 import Environment
from playwright.async_api import async_playwright
from pypdf import PdfReader

from invoice_agent.domain import InvoiceDraft, InvoiceItem, calculate_totals

SOURCE_TEMPLATE = Path(__file__).parent.parent / "invoice_template.html"
FIXED_FIELDS = (
    "company_address_line1",
    "company_address_line2",
    "company_email",
    "company_phone",
    "company_reg_no",
    "bank_account_name",
    "bank_name",
    "bank_account_no",
    "signature_name",
)
OPTIONAL_ROW_PATTERNS = {
    "outstation": re.compile(
        r'<tr class="sub-note alt">\s*<td></td><td class="col-desc">'
        r"Outstation Accommodation.*?</tr>",
        re.DOTALL,
    ),
    "rom": re.compile(
        r'<tr class="section-row alt" data-item-row="B">.*?</tr>',
        re.DOTALL,
    ),
    "floor_manager": re.compile(
        r'<tr class="section-row alt" data-item-row="D">.*?'
        r'<tr class="sub-note alt">.*?</tr>',
        re.DOTALL,
    ),
    "dj": re.compile(
        r'<tr class="section-row alt" data-item-row="E">.*?'
        r'<tr class="sub-note alt">.*?</tr>\s*'
        r'<tr class="sub-note alt">.*?</tr>',
        re.DOTALL,
    ),
}


class PdfOverflowError(ValueError):
    pass


@dataclass(frozen=True)
class RenderedPdf:
    path: Path
    digest: str
    page_count: int


def replace_field(source: str, field: str, expression: str) -> str:
    pattern = re.compile(
        rf'(?P<open><(?P<tag>[a-zA-Z0-9]+)\b[^>]*data-field="{field}"[^>]*>)'
        rf".*?(?P<close></(?P=tag)>)",
        re.DOTALL,
    )
    return pattern.sub(rf"\g<open>{expression}\g<close>", source, count=1)


def clean_fixed_tag(match: re.Match[str]) -> str:
    tag = re.sub(r'\s+contenteditable="true"', "", match.group(0))
    class_match = re.search(r'class="([^"]*)"', tag)
    if class_match is None:
        return tag
    classes = class_match.group(1).split()
    cleaned = " ".join(name for name in classes if name != "editable")
    return tag.replace(class_match.group(0), f'class="{cleaned}"')


def build_template_source(source: str) -> str:
    fields = (
        "invoice_no_title",
        "invoice_no",
        "date_issue",
        "attn_to",
        "event_date",
        "event_time",
        "event_venue",
        "event_language",
        "event_style",
        "item_A_desc",
        "item_A_qty",
        "item_A_amount",
        "item_B_desc",
        "item_B_amount",
        "item_D_desc",
        "item_D_qty",
        "item_D_amount",
        "item_E_desc",
        "item_E_qty",
        "item_E_amount",
        "total_amount",
        "booking_fee",
        "balance_due",
    )
    result = source
    for field in fields:
        result = replace_field(result, field, "{{ values." + field + " }}")
    result = result.replace(' contenteditable="true"', "")
    result = re.sub(
        r'\s*\[contenteditable="true"\]\s*\{.*?\}',
        "",
        result,
        flags=re.DOTALL,
    )
    result = re.sub(
        r"\s*\.editable:hover\s*\{.*?\}",
        "",
        result,
        flags=re.DOTALL,
    )
    for field in FIXED_FIELDS:
        pattern = re.compile(rf'<[^>]*data-field="{field}"[^>]*>')
        result = pattern.sub(clean_fixed_tag, result, count=1)
    return result


def find_item(draft: InvoiceDraft, kind: str) -> InvoiceItem | None:
    return next((item for item in draft.items if item.kind == kind), None)


def format_money(item: InvoiceItem | None) -> str:
    return f"{item.unit_price:.2f}" if item else ""


def english_names(names: str) -> str:
    names = re.sub(r"[\u3400-\u4dbf\u4e00-\u9fff]+", "", names)
    parts = re.split(
        r"\s*(?:&|:|\n|(?<!<)/|\band\b)\s*",
        names,
        flags=re.IGNORECASE,
    )
    return " & ".join(" ".join(part.split()) for part in parts if part.strip())


def format_event_date(value: str) -> str:
    normalized = re.sub(r"\s*\([^)]*\)\s*$", "", value.strip())
    normalized = re.sub(
        r"(?<=\d)(st|nd|rd|th)\b",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    formats = ("%Y-%m-%d", "%d/%m/%Y", "%d %B %Y", "%d %b %Y")
    parsed = None
    for date_format in formats:
        try:
            parsed = strptime(normalized, date_format)
            break
        except ValueError:
            continue
    if parsed is None:
        return value
    suffix = (
        "th"
        if 10 < parsed.tm_mday % 100 < 14
        else {1: "st", 2: "nd", 3: "rd"}.get(parsed.tm_mday % 10, "th")
    )
    return f"{parsed.tm_mday}{suffix} {strftime('%b %Y (%A)', parsed)}".upper()


def template_values(draft: InvoiceDraft) -> dict[str, str | int]:
    primary = find_item(draft, "primary")
    rom = find_item(draft, "rom")
    floor_manager = find_item(draft, "floor_manager")
    dj = find_item(draft, "dj")
    totals = calculate_totals(draft.items, draft.booking_fee)
    return {
        "invoice_no_title": f"Invoice {draft.invoice_number}",
        "invoice_no": draft.invoice_number,
        "date_issue": draft.issue_date.strftime("%d/%m/%Y"),
        "attn_to": (
            f"COUPLE : {english_names(draft.customer_names).upper()} "
            f"({draft.contact_number})"
        ),
        "event_date": f"Date: {format_event_date(draft.event_date)}",
        "event_time": f"Time: {draft.event_time}",
        "event_venue": f"Venue: {draft.venue}",
        "event_language": f"Language: {draft.language}",
        "event_style": (
            f"{draft.event_style} (Around {draft.table_count} tables)"
            if draft.table_count is not None
            else f"{draft.event_style} (Around {draft.pax_count} pax)"
        ),
        "item_A_desc": "Professional Emcee Hosting" if primary else "",
        "item_A_qty": primary.quantity if primary else "",
        "item_A_amount": format_money(primary),
        "item_B_desc": "ROM Hosting (Before Dinner Start)" if rom else "",
        "item_B_amount": format_money(rom),
        "item_D_desc": "Floor Manager" if floor_manager else "",
        "item_D_qty": floor_manager.quantity if floor_manager else "",
        "item_D_amount": format_money(floor_manager),
        "item_E_desc": "Wedding DJ Services" if dj else "",
        "item_E_qty": dj.quantity if dj else "",
        "item_E_amount": format_money(dj),
        "total_amount": f"{totals.total:.2f}",
        "booking_fee": f"{totals.booking_fee:.2f}",
        "balance_due": f"{totals.balance:.2f}",
    }


def add_rom_quantity(rendered: str, item: InvoiceItem | None) -> str:
    if item is None:
        return rendered
    pattern = re.compile(
        r'(<tr class="section-row alt" data-item-row="B">.*?'
        r'<td class="col-qty">)(.*?)(</td>)',
        re.DOTALL,
    )
    return pattern.sub(rf"\g<1>{item.quantity}\g<3>", rendered, count=1)


def update_outstation(rendered: str, item: InvoiceItem | None) -> str:
    if item is None:
        return rendered
    row_match = OPTIONAL_ROW_PATTERNS["outstation"].search(rendered)
    if row_match is None:
        return rendered
    row = row_match.group(0)
    description = escape(item.description)
    value = f"-{description} (RM {item.amount:.0f})"
    row = re.sub(r"Outstation Accommodation.*?</td>", value + "</td>", row, count=1)
    return rendered[: row_match.start()] + row + rendered[row_match.end() :]


def remove_unselected_rows(rendered: str, draft: InvoiceDraft) -> str:
    result = rendered
    for kind, pattern in OPTIONAL_ROW_PATTERNS.items():
        if find_item(draft, kind) is None:
            result = pattern.sub("", result, count=1)
    return result


def renumber_item_rows(rendered: str) -> str:
    pattern = re.compile(
        r'data-item-row="[A-E]".*?<td class="col-no">(?P<label>[A-E])</td>',
        re.DOTALL,
    )
    result = rendered
    matches = list(pattern.finditer(rendered))
    for index, match in reversed(list(enumerate(matches))):
        result = (
            result[: match.start("label")]
            + chr(ord("A") + index)
            + result[match.end("label") :]
        )
    return result


def render_html(draft: InvoiceDraft) -> str:
    source = SOURCE_TEMPLATE.read_text(encoding="utf-8")
    environment = Environment(autoescape=True)
    template = environment.from_string(build_template_source(source))
    rendered = template.render(values=template_values(draft))
    rendered = add_rom_quantity(rendered, find_item(draft, "rom"))
    rendered = update_outstation(rendered, find_item(draft, "outstation"))
    rendered = remove_unselected_rows(rendered, draft)
    return renumber_item_rows(rendered)


class PdfRenderer:
    async def render(
        self,
        draft: InvoiceDraft,
        destination: Path,
    ) -> RenderedPdf:
        destination.parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            await page.set_content(render_html(draft), wait_until="load")
            await page.emulate_media(media="print")
            await page.pdf(
                path=destination,
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
                scale=0.85,
            )
            await browser.close()
        page_count = len(PdfReader(destination).pages)
        if page_count != 1:
            destination.unlink(missing_ok=True)
            raise PdfOverflowError("Invoice must fit on one A4 page")
        digest = sha256(destination.read_bytes()).hexdigest()
        return RenderedPdf(destination, digest, page_count)
