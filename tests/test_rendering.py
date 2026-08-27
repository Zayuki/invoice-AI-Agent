import re
from dataclasses import replace
from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest
from playwright.async_api import async_playwright

from invoice_agent.domain import InvoiceDraft, InvoiceItem
from invoice_agent.rendering import PdfRenderer, render_html


def make_draft(items: tuple[InvoiceItem, ...] | None = None) -> InvoiceDraft:
    return InvoiceDraft(
        invoice_number="IV-2026-0001",
        issue_date=date(2026, 8, 12),
        customer_names="Yeoh Hong Shiong 杨鎽祥 & Tan Li Yin 陈丽莹",
        contact_number="+60149825136",
        event_date="29 November 2026 (Sunday)",
        event_time="Luncheon",
        venue="Sutera Pekin, Johor",
        language="Mandarin (50%) & English (50%)",
        event_style="Western",
        table_count=45,
        items=items or (InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),),
    )


def test_render_escapes_customer_values() -> None:
    draft = replace(make_draft(), customer_names="<script>alert(1)</script>")

    html = render_html(draft)

    assert "&lt;SCRIPT&gt;ALERT(1)&lt;/SCRIPT&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_render_uses_english_couple_names_only() -> None:
    html = render_html(make_draft())

    assert "COUPLE : YEOH HONG SHIONG &amp; TAN LI YIN (+60149825136)" in html
    assert "杨鎽祥" not in html
    assert "陈丽莹" not in html


@pytest.mark.parametrize(
    "customer_names",
    (
        "新郎: 许育翔 Koh Eik Siang\n新娘: 张蓓萱 Chong Bei Xuan",
        "Koh Eik Siang / Chong Bei Xuan",
        "Koh Eik Siang and Chong Bei Xuan",
        "Koh Eik Siang: Chong Bei Xuan",
    ),
)
def test_render_normalizes_couple_name_separator(customer_names: str) -> None:
    draft = replace(
        make_draft(),
        customer_names=customer_names,
        contact_number="018-3677882",
    )

    html = render_html(draft)

    assert "COUPLE : KOH EIK SIANG &amp; CHONG BEI XUAN (018-3677882)" in html


def test_event_style_and_table_count_are_the_last_event_field() -> None:
    html = render_html(make_draft())

    language = html.index('data-field="event_language"')
    event_style = html.index('data-field="event_style"')
    first_service = html.index('data-field="item_A_desc"')

    assert language < event_style < first_service
    assert 'data-field="event_style">Western (Around 45 tables)<' in html
    assert "Event Style:" not in html


def test_event_style_can_show_pax_count() -> None:
    draft = replace(make_draft(), table_count=None, pax_count=300)

    html = render_html(draft)

    assert 'data-field="event_style">Western (Around 300 pax)<' in html


def test_render_formats_issue_date_as_day_month_year() -> None:
    html = render_html(make_draft())

    assert 'data-field="date_issue">12/08/2026<' in html


def test_render_formats_wedding_date_with_ordinal_day() -> None:
    html = render_html(make_draft())

    assert 'data-field="event_date">Date: 29TH NOV 2026 (SUNDAY)<' in html


@pytest.mark.parametrize(
    ("event_date", "expected"),
    (
        ("01/04/2000", "1ST APR 2000 (SATURDAY)"),
        ("11 April 2000", "11TH APR 2000 (TUESDAY)"),
    ),
)
def test_render_uses_correct_wedding_date_ordinals(
    event_date: str,
    expected: str,
) -> None:
    html = render_html(replace(make_draft(), event_date=event_date))

    assert f'data-field="event_date">Date: {expected}<' in html


def test_optional_rows_are_omitted() -> None:
    html = render_html(make_draft())

    assert "ROM Hosting (Before Dinner Start)" not in html
    assert "Wedding DJ Services" not in html
    assert "Floor Manager" not in html
    assert "Outstation Accommodation" not in html


def test_selected_optional_rows_and_totals_are_rendered() -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),
        InvoiceItem("Floor Manager", 1, Decimal(120), "floor_manager"),
    )

    html = render_html(make_draft(items))

    assert "Floor Manager" in html
    assert ">2308.00<" in html
    assert ">1154.00<" in html


def test_outstation_destination_and_fee_render_separately_from_totals() -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal("2188.00")),
        InvoiceItem(
            "KL Outstation Accommodation & Transportation",
            1,
            Decimal("400.00"),
            "outstation",
        ),
    )

    html = render_html(make_draft(items))

    assert "- KL Outstation Accommodation &amp; Transportation (RM 400)" in html
    assert (
        'data-field="total_amount" data-formula="SUM(item_A_amount,item_B_amount,item_D_amount,item_E_amount)">2188.00<'
        in html
    )
    assert 'data-field="booking_fee">1094.00<' in html
    assert 'data-field="balance_due">1094.00<' in html


def test_service_headings_ignore_agent_descriptions() -> None:
    items = (
        InvoiceItem("Wedding Reception emcee service (25 tables)", 1, Decimal(2188)),
        InvoiceItem("Changed ROM", 1, Decimal(488), "rom"),
        InvoiceItem("Changed floor manager", 1, Decimal(120), "floor_manager"),
        InvoiceItem("Changed DJ", 1, Decimal(300), "dj"),
    )

    html = render_html(make_draft(items))

    assert "Professional Emcee Hosting" in html
    assert "ROM Hosting (Before Dinner Start)" in html
    assert "Program Planning &amp; Management" in html
    assert "Floor Manager" in html
    assert "Wedding DJ Services" in html
    assert "Wedding Reception emcee service" not in html
    assert "Changed" not in html


def test_manual_booking_fee_updates_rendered_balance_only() -> None:
    draft = replace(make_draft(), booking_fee=Decimal("800.00"))

    html = render_html(draft)

    assert (
        'data-field="total_amount" data-formula="SUM(item_A_amount,item_B_amount,item_D_amount,item_E_amount)">2188.00<'
        in html
    )
    assert 'data-field="booking_fee">800.00<' in html
    assert 'data-field="balance_due">1388.00<' in html


def test_rendered_item_numbers_are_sequential() -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),
        InvoiceItem("Floor Manager", 1, Decimal(120), "floor_manager"),
    )

    html = render_html(make_draft(items))
    numbers = re.findall(
        r'data-item-row="[A-E]">\s*<td class="col-no">([A-E])',
        html,
    )

    assert numbers == ["A", "B", "C"]


def test_fixed_business_fields_are_not_editable_or_yellow() -> None:
    html = render_html(make_draft())

    assert 'data-field="company_reg_no"' in html
    assert 'contenteditable="true"' not in html
    assert 'class="meta-value" data-field="company_reg_no"' in html


@pytest.mark.asyncio
async def test_print_cells_have_white_backgrounds() -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),
        InvoiceItem("ROM Hosting", 1, Decimal(488), "rom"),
        InvoiceItem("Floor Manager", 1, Decimal(120), "floor_manager"),
        InvoiceItem("Wedding DJ Services", 1, Decimal(300), "dj"),
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft(items)))
        await page.emulate_media(media="print")
        editable_colors = await page.locator(".editable").evaluate_all(
            "elements => [...new Set(elements.map(element => "
            "getComputedStyle(element).backgroundColor))]"
        )
        alternate_colors = await page.locator(".items-table tr.alt > td").evaluate_all(
            "elements => [...new Set(elements.map(element => "
            "getComputedStyle(element).backgroundColor))]"
        )
        await browser.close()

    assert editable_colors == ["rgb(255, 255, 255)"]
    assert alternate_colors == ["rgb(255, 255, 255)"]


@pytest.mark.asyncio
async def test_reference_table_uses_white_rows() -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),
        InvoiceItem("ROM Hosting", 1, Decimal(488), "rom"),
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft(items)))
        colors = await page.locator(".items-table tr.alt > td").evaluate_all(
            "elements => [...new Set(elements.map(element => "
            "getComputedStyle(element).backgroundColor))]"
        )
        await browser.close()

    assert colors == ["rgb(255, 255, 255)"]


@pytest.mark.asyncio
async def test_reference_table_uses_reference_column_layout() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        table_width = await page.locator(".items-table").evaluate(
            "element => element.getBoundingClientRect().width"
        )
        column_widths = await page.locator(".items-table th").evaluate_all(
            "elements => elements.map(element => element.getBoundingClientRect().width)"
        )
        await browser.close()

    proportions = [width / table_width for width in column_widths]
    assert proportions == pytest.approx([0.11, 0.45, 0.07, 0.26, 0.11], abs=0.01)


@pytest.mark.asyncio
async def test_reference_table_wraps_total_header() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        total_header = await page.locator(".items-table th.col-total").inner_text()
        await browser.close()

    assert total_header == "Total Amount\n(RM)"


@pytest.mark.asyncio
async def test_reference_table_aligns_service_notes() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        styles = await page.locator(
            '.items-table [data-item-row="A"] > .col-desc, '
            ".items-table .sub-note > .col-desc"
        ).evaluate_all(
            "elements => elements.map(element => { "
            "const style = getComputedStyle(element); "
            "return [style.paddingLeft, style.paddingTop]; })"
        )
        text_indents = await page.locator(
            ".items-table .sub-note > .col-desc"
        ).evaluate_all(
            "elements => elements.map(element => getComputedStyle(element).textIndent)"
        )
        await browser.close()

    assert styles[0] == ["6px", "13px"]
    assert all(style[0] == "14px" for style in styles[1:])

    assert all(indent == "-8px" for indent in text_indents)


@pytest.mark.asyncio
async def test_service_sections_have_one_line_spacing() -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),
        InvoiceItem(
            "KL Outstation Accommodation & Transportation",
            1,
            Decimal(300),
            "outstation",
        ),
        InvoiceItem("ROM Hosting", 1, Decimal(488), "rom"),
        InvoiceItem("Floor Manager", 1, Decimal(120), "floor_manager"),
        InvoiceItem("Wedding DJ Services", 1, Decimal(300), "dj"),
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft(items)))
        section_padding = await page.locator(
            ".items-table [data-item-row] > td"
        ).evaluate_all(
            "elements => [...new Set(elements.map(element => "
            "getComputedStyle(element).paddingTop))]"
        )
        note_padding = await page.locator(".items-table .sub-note > td").evaluate_all(
            "elements => [...new Set(elements.map(element => "
            "getComputedStyle(element).paddingTop))]"
        )
        await browser.close()

    assert section_padding == ["13px"]
    assert note_padding == ["1px"]


@pytest.mark.asyncio
async def test_reference_totals_use_reference_emphasis() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        weights = await page.locator(
            ".totals-table .label, .totals-table .value"
        ).evaluate_all(
            "elements => elements.map(element => getComputedStyle(element).fontWeight)"
        )
        await browser.close()

    assert weights == ["400", "700", "700", "700", "700", "700"]


@pytest.mark.asyncio
async def test_invoice_has_two_pink_dividers() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        divider_colors = await page.locator("hr.divider, .footer").evaluate_all(
            "elements => elements.map(element => "
            "getComputedStyle(element).borderTopColor)"
        )
        divider_widths = await page.locator("hr.divider, .footer").evaluate_all(
            "elements => elements.map(element => "
            "getComputedStyle(element).borderTopWidth)"
        )
        await browser.close()

    assert divider_colors == ["rgb(239, 127, 137)", "rgb(239, 127, 137)"]
    assert divider_widths == ["1px", "1px"]


@pytest.mark.asyncio
async def test_invoice_spacing_and_totals_styles() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        styles = await page.locator(
            ".attn-row, .totals-table, .totals-table .label"
        ).evaluate_all(
            "elements => elements.map(element => { "
            "const style = getComputedStyle(element); "
            "return [style.marginTop, style.marginBottom, "
            "style.borderTopWidth, style.borderTopColor, "
            "style.whiteSpace, style.paddingTop]; })"
        )
        await browser.close()

    assert styles[0][0:2] == ["40px", "40px"]
    assert styles[1][2:4] == ["1px", "rgb(0, 0, 0)"]
    assert all(style[4] == "nowrap" for style in styles[2:])
    assert styles[2][5] == "8px"


@pytest.mark.asyncio
async def test_signature_uses_edwardian_script() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        page = await browser.new_page()
        await page.set_content(render_html(make_draft()))
        font_family = await page.locator(".signature .name").evaluate(
            "element => getComputedStyle(element).fontFamily"
        )
        await browser.close()

    assert font_family == '"Edwardian Script ITC", cursive'


@pytest.mark.asyncio
async def test_pdf_is_exactly_one_page(tmp_path) -> None:
    destination = tmp_path / "invoice.pdf"

    rendered = await PdfRenderer().render(make_draft(), destination)

    assert rendered.path == destination
    assert rendered.digest == sha256(destination.read_bytes()).hexdigest()
    assert rendered.page_count == 1


@pytest.mark.asyncio
async def test_pdf_with_all_optional_services_is_one_page(tmp_path) -> None:
    items = (
        InvoiceItem("Professional Emcee Hosting", 1, Decimal(2188)),
        InvoiceItem(
            "KL Outstation Accommodation & Transportation",
            1,
            Decimal(300),
            "outstation",
        ),
        InvoiceItem("ROM Hosting", 1, Decimal(488), "rom"),
        InvoiceItem("Floor Manager", 1, Decimal(120), "floor_manager"),
        InvoiceItem("Wedding DJ Services", 1, Decimal(300), "dj"),
    )

    draft = replace(
        make_draft(items),
        customer_names="Alexandra Example & Benjamin Sample",
        venue=(
            "A Long Wedding Venue Name in Petaling Jaya, Selangor, Malaysia "
            "With Grand Ballroom"
        ),
        table_count=None,
        pax_count=160,
    )

    rendered = await PdfRenderer().render(draft, tmp_path / "invoice.pdf")

    assert rendered.page_count == 1
