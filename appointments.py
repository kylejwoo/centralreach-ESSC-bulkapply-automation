from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Optional

from playwright.sync_api import BrowserContext

from models import ClientPayment
import table_parser
from table_parser import detect_billing_table


LAST_DIAGNOSTICS: dict[str, Any] = {}


def normalize_date(text: str) -> Optional[str]:
    match = re.search(
        r"(?<!\d)(0?\d{1,2})/(0?\d{1,2})/(\d{2}|\d{4})(?!\d)", str(text)
    )
    if not match:
        return None
    month, day, year = match.groups()
    year = "20" + year if len(year) == 2 else year
    try:
        return datetime(int(year), int(month), int(day)).strftime("%-m/%-d/%Y")
    except ValueError:
        return None


def parse_money(text: str) -> Optional[float]:
    cleaned = str(text).replace("$", "").replace(",", "").strip()
    match = re.search(r"-?\d+(?:\.\d{1,2})?", cleaned)
    if not match:
        return None
    try:
        return round(float(match.group()), 2)
    except ValueError:
        return None


def clear_selected_appointments(context: BrowserContext) -> None:
    for page in context.pages:
        if page.is_closed():
            continue
        for frame in page.frames:
            checked = frame.locator('input[type="checkbox"]:checked')
            for index in range(checked.count()):
                try:
                    checkbox = checked.nth(index)
                    if checkbox.is_visible():
                        checkbox.uncheck()
                except Exception:
                    continue


def billing_rows(context: BrowserContext) -> list[dict[str, Any]]:
    global LAST_DIAGNOSTICS
    try:
        table, mapping = detect_billing_table(context)
    except Exception:
        LAST_DIAGNOSTICS = {"table_parser": table_parser.LAST_TABLE_DIAGNOSTICS}
        raise
    rows = table.locator("tbody tr:visible")
    output: list[dict[str, Any]] = []

    for row_index in range(rows.count()):
        row = rows.nth(row_index)
        checkbox = row.locator('input[type="checkbox"]')
        if checkbox.count() == 0:
            continue
        cells = row.locator("td, th")
        if cells.count() <= max(mapping.date_index, mapping.charges_billed_index):
            continue
        try:
            date_text = cells.nth(mapping.date_index).inner_text()
            amount_text = cells.nth(mapping.charges_billed_index).inner_text()
            row_text = " ".join(row.inner_text().split())
        except Exception:
            continue
        date = normalize_date(date_text)
        amount = parse_money(amount_text)
        if date is None or amount is None:
            continue
        output.append(
            {
                "row_index": row_index,
                "row": row,
                "checkbox": checkbox.first,
                "date": date,
                "amount": amount,
                "date_text": date_text.strip(),
                "amount_text": amount_text.strip(),
                "row_text": row_text,
            }
        )

    LAST_DIAGNOSTICS = {
        "table_parser": table_parser.LAST_TABLE_DIAGNOSTICS,
        "column_map": mapping.as_dict(),
        "rows": [
            {
                "row_index": item["row_index"],
                "date": item["date"],
                "charges_billed": item["amount"],
                "date_text": item["date_text"],
                "amount_text": item["amount_text"],
                "row_text": item["row_text"],
            }
            for item in output
        ],
    }
    return output


def inspect_billing_table(context: BrowserContext) -> dict[str, Any]:
    rows = billing_rows(context)
    print("\nDetected CentralReach billing columns")
    mapping = LAST_DIAGNOSTICS["column_map"]
    print(f"Date: index {mapping['date_index']} -> {mapping['leaf_paths'][mapping['date_index']]}")
    print(
        "Charges Billed: index "
        f"{mapping['charges_billed_index']} -> "
        f"{mapping['leaf_paths'][mapping['charges_billed_index']]}"
    )
    print(f"Appointment rows parsed: {len(rows)}")
    for row in rows[:10]:
        print(f"  {row['date']} | ${row['amount']:.2f}")
    return LAST_DIAGNOSTICS


def verify_expected_appointments(
    context: BrowserContext,
    client: ClientPayment,
) -> tuple[bool, str]:
    """Check whether the current profile can satisfy every Excel appointment."""
    try:
        rows = billing_rows(context)
    except Exception as exc:
        return False, f"billing table could not be read: {exc}"

    used: set[int] = set()
    missing: list[str] = []
    for expected in client.appointments:
        match_index = next(
            (
                index
                for index, row in enumerate(rows)
                if index not in used
                and row["date"] == expected.date
                and abs(row["amount"] - expected.submitted_amount) <= 0.01
            ),
            None,
        )
        if match_index is None:
            available = [
                row["amount"]
                for index, row in enumerate(rows)
                if index not in used and row["date"] == expected.date
            ]
            missing.append(
                f"{expected.date} ${expected.submitted_amount:.2f} "
                f"(available: {available or 'none'})"
            )
        else:
            used.add(match_index)

    if missing:
        return False, "missing appointment match(es): " + ", ".join(missing)
    matched_total = round(sum(rows[index]["amount"] for index in used), 2)
    if len(used) != client.total_appointments:
        return False, f"matched {len(used)} of {client.total_appointments} appointments"
    if abs(matched_total - client.expected_total) > 0.01:
        return False, (
            f"matched total ${matched_total:.2f}, expected ${client.expected_total:.2f}"
        )
    return True, (
        f"matched {len(used)} appointment(s) totaling ${matched_total:.2f}"
    )


def select_appointments(context: BrowserContext, client: ClientPayment) -> int:
    clear_selected_appointments(context)
    rows = billing_rows(context)
    used: set[int] = set()

    for expected in client.appointments:
        matches = [
            (index, row)
            for index, row in enumerate(rows)
            if index not in used
            and row["date"] == expected.date
            and abs(row["amount"] - expected.submitted_amount) <= 0.01
        ]
        available = [
            row["amount"]
            for index, row in enumerate(rows)
            if index not in used and row["date"] == expected.date
        ]
        logging.info(
            "%s | expected %s $%.2f | available Charges>Billed=%s",
            client.client,
            expected.date,
            expected.submitted_amount,
            available,
        )
        if not matches:
            raise RuntimeError(
                f"{client.client}: no appointment matched {expected.date} at "
                f"${expected.submitted_amount:.2f}. Available CHARGES > Billed "
                f"amounts on that date: {available or 'none'}"
            )
        index, row = matches[0]
        row["checkbox"].scroll_into_view_if_needed()
        row["checkbox"].check()
        used.add(index)

    selected_total = round(sum(rows[index]["amount"] for index in used), 2)
    if len(used) != client.total_appointments:
        raise RuntimeError(
            f"{client.client}: selected {len(used)}, expected {client.total_appointments}."
        )
    if abs(selected_total - client.expected_total) > 0.01:
        raise RuntimeError(
            f"{client.client}: selected Charges total ${selected_total:.2f}, "
            f"expected Submitted_amount total ${client.expected_total:.2f}."
        )
    logging.info(
        "%s selected %d appointments totaling %.2f",
        client.client,
        len(used),
        selected_total,
    )
    return len(used)
