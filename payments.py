from __future__ import annotations
import logging,re,time
from datetime import datetime
from typing import Optional
from playwright.sync_api import BrowserContext, Locator, Page
from config import PAYMENT_TYPE_OPTION
from browser_utils import all_frames, visible_locator, newest_page, screenshot, find_text_button
from models import ClientPayment
from appointments import parse_money

def open_bulk_apply(context: BrowserContext) -> Page:
    pattern = re.compile(r"Bulk[\s-]*Apply Payments", re.IGNORECASE)
    button = find_text_button(context, pattern)

    if button is None:
        page = newest_page(context)
        screenshot(page, "bulk_apply_button_not_found")
        raise RuntimeError("Could not find the Bulk Apply Payments button.")

    button.click()
    page = newest_page(context)
    page.wait_for_timeout(1500)

    return page

def find_input_near_text(
    context: BrowserContext,
    label_text: str,
) -> Optional[Locator]:
    exact_pattern = re.compile(rf"^{re.escape(label_text)}$", re.IGNORECASE)

    for frame in all_frames(context):
        labeled = visible_locator(frame.get_by_label(exact_pattern))

        if labeled is not None:
            return labeled

        label = visible_locator(frame.get_by_text(exact_pattern))

        if label is None:
            continue

        parent = label.locator(
            "xpath=ancestor::*[self::div or self::td or self::label][1]"
        )
        nearby = visible_locator(parent.locator("input"))

        if nearby is not None:
            return nearby

        following = visible_locator(
            label.locator("xpath=following::input[1]")
        )

        if following is not None:
            return following

    return None

def fill_labeled_input(
    context: BrowserContext,
    label_text: str,
    value: str,
) -> None:
    field = find_input_near_text(context, label_text)

    if field is None:
        raise RuntimeError(f"Could not locate the {label_text} input.")

    field.scroll_into_view_if_needed()
    field.click()
    field.fill("")
    field.fill(value)

def select_payment_type(context: BrowserContext) -> None:
    payment_pattern = re.compile(r"Payment Type", re.IGNORECASE)

    for frame in all_frames(context):
        select = visible_locator(frame.get_by_label(payment_pattern))

        if select is not None:
            try:
                select.select_option(label=PAYMENT_TYPE_OPTION)
                return
            except Exception:
                pass

        native_select = visible_locator(
            frame.locator("select").filter(
                has=frame.locator(
                    f'option:has-text("{PAYMENT_TYPE_OPTION}")'
                )
            )
        )

        if native_select is not None:
            native_select.select_option(label=PAYMENT_TYPE_OPTION)
            return

    for frame in all_frames(context):
        payment_label = visible_locator(frame.get_by_text(payment_pattern))

        if payment_label is None:
            continue

        payment_label.click()

        option = visible_locator(
            frame.get_by_text(PAYMENT_TYPE_OPTION, exact=True)
        )

        if option is not None:
            option.click()
            return

    raise RuntimeError("Could not select Electronic as the Payment Type.")

def set_native_input_value(element: Locator, value: str) -> None:
    """
    Set an input value using the browser's native setter and dispatch the
    events used by React/Angular-style forms.
    """
    element.evaluate(
        """(input, value) => {
            const prototype =
                input instanceof HTMLTextAreaElement
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;

            const descriptor = Object.getOwnPropertyDescriptor(
                prototype,
                'value'
            );

            if (descriptor && descriptor.set) {
                descriptor.set.call(input, value);
            } else {
                input.value = value;
            }

            input.dispatchEvent(
                new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                })
            );
            input.dispatchEvent(new Event('change', { bubbles: true }));
            input.dispatchEvent(new Event('blur', { bubbles: true }));
        }""",
        value,
    )

def find_bulk_payment_control(
    context: BrowserContext,
    header_name: str,
    control_selector: str,
) -> Optional[Locator]:
    """
    Find a control in the top 'Apply bulk payments' table by matching its
    column header, then selecting the control in the cell directly below it.

    This avoids the old nearest-label fallback, which could accidentally
    select the Date field for Reference or Notes.
    """
    expected = header_name.strip().lower()

    for frame in all_frames(context):
        tables = frame.locator("table:visible")

        for table_index in range(tables.count()):
            table = tables.nth(table_index)

            try:
                table_text = " ".join(table.inner_text().split()).lower()
            except Exception:
                continue

            # The correct top form table contains these headers.
            if "payment type" not in table_text or "reference" not in table_text:
                continue
            if "notes" not in table_text or "running total" not in table_text:
                continue

            headers = table.locator("thead th, thead td, tr:first-child th, tr:first-child td")
            header_index = None

            for index in range(headers.count()):
                try:
                    header_text = " ".join(headers.nth(index).inner_text().split()).lower()
                except Exception:
                    continue

                if header_text == expected:
                    header_index = index
                    break

            if header_index is None:
                continue

            # Prefer a tbody row. Fall back to the second table row.
            rows = table.locator("tbody tr")
            if rows.count() == 0:
                rows = table.locator("tr").nth(1)

            if hasattr(rows, "count") and rows.count() > 0:
                row = rows.first
            else:
                row = rows

            cells = row.locator("td, th")

            if cells.count() <= header_index:
                continue

            cell = cells.nth(header_index)
            control = visible_locator(cell.locator(control_selector))

            if control is not None:
                logging.info(
                    "Found %s control in bulk payment table column %d",
                    header_name,
                    header_index,
                )
                return control

    return None

def fill_payment_form(
    context: BrowserContext,
    client: ClientPayment,
) -> None:
    page = newest_page(context)

    # Close an open date picker before interacting with the fields.
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass

    display_date = datetime.strptime(
        client.payment_date,
        "%Y-%m-%d",
    ).strftime("%m/%d/%Y")

    date_input = find_bulk_payment_control(
        context,
        "Date",
        'input:not([type="file"])',
    )
    reference_input = find_bulk_payment_control(
        context,
        "Reference",
        'input:not([type="file"])',
    )
    notes_input = find_bulk_payment_control(
        context,
        "Notes",
        'input:not([type="file"]), textarea',
    )
    payment_select = find_bulk_payment_control(
        context,
        "Payment type",
        "select",
    )

    missing = []
    if date_input is None:
        missing.append("Date")
    if payment_select is None:
        missing.append("Payment type")
    if reference_input is None:
        missing.append("Reference")
    if notes_input is None:
        missing.append("Notes")

    if missing:
        screenshot(page, "bulk_payment_controls_not_found")
        raise RuntimeError(
            "Could not locate these bulk payment fields: "
            + ", ".join(missing)
        )

    # Fill each exact column control without clicking the date picker.
    set_native_input_value(date_input, display_date)

    try:
        payment_select.select_option(label=PAYMENT_TYPE_OPTION)
    except Exception:
        payment_select.select_option(value=PAYMENT_TYPE_OPTION)

    set_native_input_value(reference_input, client.payment_number)
    set_native_input_value(notes_input, client.invoice_number)

    page.wait_for_timeout(800)

    # Read back the exact controls to make sure no field was overwritten.
    actual_date = date_input.evaluate("(element) => element.value || ''")
    actual_reference = reference_input.evaluate("(element) => element.value || ''")
    actual_notes = notes_input.evaluate("(element) => element.value || ''")

    logging.info(
        "Bulk payment fields | date=%r | reference=%r | notes=%r",
        actual_date,
        actual_reference,
        actual_notes,
    )

    if actual_reference.strip() != client.payment_number:
        raise RuntimeError(
            f"Reference field contains {actual_reference!r}, "
            f"expected {client.payment_number!r}."
        )

    if actual_notes.strip() != client.invoice_number:
        raise RuntimeError(
            f"Notes field contains {actual_notes!r}, "
            f"expected {client.invoice_number!r}."
        )

def verify_payment_form(
    context: BrowserContext,
    client: ClientPayment,
) -> None:
    """
    Verify values by reading the actual form controls.

    Input values are not included in body.inner_text(), which caused the
    earlier false "Missing values" error even though the fields were filled.
    """
    page = newest_page(context)

    reference_input = find_bulk_payment_control(
        context,
        "Reference",
        'input:not([type="file"])',
    )
    notes_input = find_bulk_payment_control(
        context,
        "Notes",
        'input:not([type="file"]), textarea',
    )
    payment_select = find_bulk_payment_control(
        context,
        "Payment type",
        "select",
    )

    if reference_input is None or notes_input is None or payment_select is None:
        screenshot(page, "payment_form_verification_controls_missing")
        raise RuntimeError(
            "Could not re-locate Reference, Notes, or Payment Type "
            "for final verification."
        )

    actual_reference = reference_input.evaluate(
        "(element) => String(element.value || '')"
    ).strip()
    actual_notes = notes_input.evaluate(
        "(element) => String(element.value || '')"
    ).strip()

    selected_payment_type = payment_select.evaluate(
        """(element) => {
            const option = element.options[element.selectedIndex];
            return option ? String(option.text || option.value || '') : '';
        }"""
    ).strip()

    logging.info(
        "Final payment verification | reference=%r | notes=%r | type=%r",
        actual_reference,
        actual_notes,
        selected_payment_type,
    )

    errors = []

    if actual_reference != client.payment_number:
        errors.append(
            f"Reference is {actual_reference!r}, expected "
            f"{client.payment_number!r}"
        )

    if actual_notes != client.invoice_number:
        errors.append(
            f"Notes is {actual_notes!r}, expected "
            f"{client.invoice_number!r}"
        )

    if PAYMENT_TYPE_OPTION.lower() not in selected_payment_type.lower():
        errors.append(
            f"Payment Type is {selected_payment_type!r}, expected "
            f"{PAYMENT_TYPE_OPTION!r}"
        )

    if errors:
        screenshot(page, "payment_form_verification_failed")
        raise RuntimeError("; ".join(errors))

def verify_bulk_total(context: BrowserContext, client: ClientPayment) -> None:
    """Read the charge total cell from the Grand total row.

    The old version parsed the entire row and mistook the first value, 7.00
    hours, for the $741.00 payment total. In CentralReach's bulk table, the
    charge total is the fifth visible cell in the Grand total row.
    """
    found = None
    diagnostic_text = ""

    for frame in all_frames(context):
        rows = frame.locator("tr:visible")
        for index in range(rows.count()):
            row = rows.nth(index)
            try:
                text = " ".join(row.inner_text().split())
            except Exception:
                continue
            if "grand total" not in text.lower():
                continue

            diagnostic_text = text
            cells = row.locator("td:visible, th:visible")
            values = []
            for cell_index in range(cells.count()):
                try:
                    cell_text = " ".join(cells.nth(cell_index).inner_text().split())
                except Exception:
                    continue
                amount = parse_money(cell_text)
                values.append((cell_index, cell_text, amount))

            # CentralReach columns: label, hours, drive hours, mileage, charges,
            # copay, amount paid, amount owed. The charges cell is index 4.
            if len(values) > 4 and values[4][2] is not None:
                found = values[4][2]
            else:
                # Defensive fallback: use a value that matches the expected
                # payment total rather than the first number in the row.
                matching = [
                    amount for _, _, amount in values
                    if amount is not None and abs(amount - client.expected_total) <= 0.01
                ]
                if matching:
                    found = matching[0]

            logging.info(
                "Grand total row parsed for %s | row=%r | cells=%r | selected=%r",
                client.client,
                diagnostic_text,
                values,
                found,
            )
            break
        if found is not None:
            break

    if found is None:
        raise RuntimeError(
            "Could not read the charge total from the Grand total row on the "
            "bulk payment page."
        )
    if abs(found - client.expected_total) > 0.01:
        raise RuntimeError(
            f"{client.client}: CR Grand total ${found:.2f} does not match "
            f"expected ${client.expected_total:.2f}. Row: {diagnostic_text}"
        )

def apply_or_preview(context: BrowserContext, client: ClientPayment, commit: bool) -> str:
    page = newest_page(context)
    verify_payment_form(context, client)
    screenshot(page, f"ready_{client.client}")

    if not commit:
        print(f"\nDRY RUN READY FOR: {client.client}\nApply Payments was not clicked.")
        return "previewed"

    button = find_text_button(context, re.compile(r"^Apply Payments$", re.I))
    if button is None:
        raise RuntimeError("Could not find the final Apply Payments button.")

    button.click()
    logging.info("Clicked Apply Payments for %s", client.client)

    success_patterns = [
        re.compile(r"payment(?:s)? (?:was|were)? ?applied", re.I),
        re.compile(r"successfully applied", re.I),
        re.compile(r"payment successful", re.I),
    ]
    error_patterns = [
        re.compile(r"unable to apply", re.I),
        re.compile(r"payment(?:s)? failed", re.I),
        re.compile(r"submission failed", re.I),
    ]

    deadline = time.time() + 20
    while time.time() < deadline:
        for frame in all_frames(context):
            # Only inspect alert/toast regions for errors to avoid matching
            # unrelated page text such as field instructions.
            alert_texts = []
            for selector in (
                "[role='alert']:visible",
                ".alert-danger:visible",
                ".toast-error:visible",
                ".notification-error:visible",
            ):
                try:
                    locator = frame.locator(selector)
                    for index in range(min(locator.count(), 10)):
                        alert_texts.append(locator.nth(index).inner_text(timeout=300))
                except Exception:
                    continue

            combined_alerts = " ".join(alert_texts)
            if combined_alerts:
                for pattern in error_patterns:
                    match = pattern.search(combined_alerts)
                    if match:
                        screenshot(newest_page(context), f"submit_error_{client.client}")
                        raise RuntimeError(
                            "CentralReach showed a submission error after Apply Payments: "
                            + combined_alerts[:300]
                        )
                if any(pattern.search(combined_alerts) for pattern in success_patterns):
                    screenshot(newest_page(context), f"submitted_{client.client}")
                    return "completed"

            # Some CentralReach success messages render outside alert containers.
            try:
                body_text = frame.locator("body").inner_text(timeout=300)
            except Exception:
                body_text = ""
            if body_text and any(pattern.search(body_text) for pattern in success_patterns):
                screenshot(newest_page(context), f"submitted_{client.client}")
                return "completed"

        # A successful submission normally returns to Billing. Treat the
        # reappearance of the Billing search box as confirmation.
        try:
            from navigation import find_search_box
            search_box, _ = find_search_box(context)
            if search_box is not None:
                screenshot(newest_page(context), f"submitted_{client.client}")
                return "completed"
        except Exception:
            pass

        newest_page(context).wait_for_timeout(500)

    screenshot(newest_page(context), f"submit_uncertain_{client.client}")
    raise RuntimeError(
        f"Submission status is uncertain for {client.client}. CentralReach did "
        "not show a clear success message or return to Billing within 20 seconds. "
        "Automation stopped to avoid a duplicate payment."
    )
