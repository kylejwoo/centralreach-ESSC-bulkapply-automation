from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext

import appointments
from browser_utils import newest_page, screenshot
from debug_bundle import create_debug_bundle
from models import ClientPayment
from navigation import ClientVerificationError, return_to_billing, search_client
from payments import apply_or_preview, fill_payment_form, open_bulk_apply, verify_bulk_total
from storage import (
    append_report,
    archive_completed,
    migrate_pending_state_name,
    payment_key,
    payment_label,
    save_state,
    update_client_name_in_json,
)


def process_client(context: BrowserContext, client: ClientPayment, commit: bool) -> str:
    search_client(
        context,
        client.client,
        verifier=lambda: appointments.verify_expected_appointments(context, client),
    )
    appointments.select_appointments(context, client)
    open_bulk_apply(context)
    verify_bulk_total(context, client)
    fill_payment_form(context, client)
    return apply_or_preview(context, client, commit)


def print_client_summary(client: ClientPayment, index: int, total: int) -> None:
    print("\n" + "=" * 70)
    print(f"Client {index + 1} of {total}: {client.client}")
    print(f"Payment date: {client.payment_date}")
    print(f"Payment number: {client.payment_number}")
    print(f"Invoice number: {client.invoice_number}")
    print(f"Appointments: {client.total_appointments}")
    print(f"Expected total: ${client.expected_total:.2f}")
    for appt in client.appointments:
        print(f"  {appt.date}: ${appt.submitted_amount:.2f}")
    print("=" * 70)


def mark_completed(state: dict[str, Any], client: ClientPayment) -> None:
    key = payment_key(client)
    state.setdefault("completed", {})[key] = {
        "client": client.client,
        "timestamp": datetime.now().isoformat(),
        "appointments": client.total_appointments,
        "payment_number": client.payment_number,
        "invoice_number": client.invoice_number,
        "payment_date": client.payment_date,
        "expected_total": client.expected_total,
    }
    state.setdefault("failed", {}).pop(key, None)
    state.setdefault("skipped", {}).pop(key, None)
    state.setdefault("blocked", {}).pop(key, None)
    save_state(state)
    archive_completed(client)
    append_report(client, "completed")


def mark_skipped(
    state: dict[str, Any],
    client: ClientPayment,
    reason: str,
    tried_candidates: str = "",
) -> None:
    key = payment_key(client)
    state.setdefault("skipped", {})[key] = {
        "client": client.client,
        "timestamp": datetime.now().isoformat(),
        "reason": reason,
        "tried_candidates": tried_candidates,
        "payment_number": client.payment_number,
        "invoice_number": client.invoice_number,
        "payment_date": client.payment_date,
        "appointments": client.total_appointments,
        "expected_total": client.expected_total,
    }
    state.setdefault("failed", {}).pop(key, None)
    save_state(state)
    append_report(client, "skipped", reason)


def print_run_summary(state: dict[str, Any], run_results: list[dict[str, str]]) -> None:
    if not run_results:
        return
    completed = [item for item in run_results if item["status"] == "completed"]
    incomplete = [item for item in run_results if item["status"] != "completed"]
    print("\n" + "=" * 70)
    print("RUN SUMMARY")
    print("=" * 70)
    print(f"Completed successfully: {len(completed)}")
    print(f"Not completed: {len(incomplete)}")
    if incomplete:
        print("\nClients not completed:")
        for item in incomplete:
            print(f"  - {item['client']}: {item['reason']}")
    print("\nDetails were saved to progress.json and run_summary.csv.")
    print("=" * 70)


def run_one(context: BrowserContext, client: ClientPayment, state: dict[str, Any], commit: bool) -> str:
    try:
        return_to_billing(context)
        result = process_client(context, client, commit)
        if result == "completed":
            mark_completed(state, client)
        return result
    except KeyboardInterrupt:
        logging.warning("Run cancelled by user while processing %s", client.client)
        print(f"\nCancelled while processing {client.client}. No completion was recorded.")
        return "cancelled"
    except ClientVerificationError as exc:
        reason = str(exc)
        logging.warning("Blocked on %s: %s", client.client, reason)
        key = payment_key(client)
        state.setdefault("blocked", {})[key] = {
            "client": client.client,
            "timestamp": datetime.now().isoformat(),
            "reason": reason,
            "payment_number": client.payment_number,
            "invoice_number": client.invoice_number,
            "payment_date": client.payment_date,
            "appointments": client.total_appointments,
            "expected_total": client.expected_total,
        }
        save_state(state)
        append_report(client, "blocked", reason)
        print(f"Could not verify {client.client}: {reason}")
        return "blocked"
    except Exception as exc:
        logging.exception("Failed %s", client.client)
        screenshot(newest_page(context), f"error_{client.client}")
        bundle = create_debug_bundle(
            context,
            client,
            exc,
            diagnostics=appointments.LAST_DIAGNOSTICS,
        )
        key = payment_key(client)
        state.setdefault("failed", {})[key] = {
            "client": client.client,
            "timestamp": datetime.now().isoformat(),
            "payment_number": client.payment_number,
            "invoice_number": client.invoice_number,
            "payment_date": client.payment_date,
            "error": str(exc),
            "debug_bundle": str(bundle) if bundle else "",
        }
        save_state(state)
        append_report(client, "failed", str(exc))
        print(f"Error for {client.client}: {exc}")
        if bundle:
            print(f"Debug bundle: {bundle}")
        return "failed"


def choose_client_index(clients: list[ClientPayment], current_index: int) -> int:
    name = input("Type exact client name or part: ").strip().lower()
    matches = [
        (index, client)
        for index, client in enumerate(clients)
        if name in client.client.lower()
    ]
    if not matches:
        print("No matching client.")
        return current_index
    if len(matches) == 1:
        return matches[0][0]
    for number, (_, client) in enumerate(matches, 1):
        print(f"{number}. {payment_label(client)}")
    try:
        return matches[int(input("Choose: ")) - 1][0]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return current_index


def _result_reason(state: dict[str, Any], client: ClientPayment, result: str) -> str:
    key = payment_key(client)
    record = (
        state.get("blocked", {}).get(key, {})
        or state.get("failed", {}).get(key, {})
        or state.get("skipped", {}).get(key, {})
    )
    return str(record.get("reason") or record.get("error") or result)


def interactive_loop(
    context: BrowserContext,
    clients: list[ClientPayment],
    state: dict[str, Any],
    commit: bool,
    start_index: int,
    batch_size: int,
    input_path: Path,
) -> None:
    """Wait for commands, then run a client or batch without inner prompts.

    The user confirms startup once and controls when work begins from this menu.
    After R or A is selected, no Enter confirmations occur during processing.
    A batch stops immediately if any client is uncertain, blocked, cancelled, or
    failed so the problem can be reviewed before later clients are touched.
    """
    completed_keys = set(state.get("completed", {}))
    pending = [client for client in clients if payment_key(client) not in completed_keys]

    if completed_keys:
        print(f"\nSkipping {len(completed_keys)} completed payment run(s).")
        print(f"Remaining clients: {len(pending)}")
    if not pending:
        print("All clients already completed.")
        return

    current = min(start_index, len(pending) - 1)
    run_results: list[dict[str, str]] = []

    while pending:
        client = pending[current]
        print_client_summary(client, current, len(pending))
        command = input(
            f"\nR=run one  A=batch {batch_size}  E=edit name + retry  "
            "S=set batch size  I=inspect table  N=next  P=previous  "
            "C=choose  B=billing  Q=quit: "
        ).strip().lower()

        if command == "q":
            print_run_summary(state, run_results)
            return
        if command == "n":
            current = min(current + 1, len(pending) - 1)
            continue
        if command == "p":
            current = max(current - 1, 0)
            continue
        if command == "c":
            current = choose_client_index(pending, current)
            continue
        if command == "s":
            raw_size = input("Enter new batch size: ").strip()
            try:
                new_size = int(raw_size)
                if new_size < 1:
                    raise ValueError
            except ValueError:
                print("Batch size must be a positive whole number.")
                continue
            batch_size = new_size
            print(f"Batch size changed to {batch_size}.")
            continue
        if command == "b":
            try:
                return_to_billing(context)
            except Exception as exc:
                print(f"Could not return to Billing: {exc}")
            continue
        if command == "i":
            try:
                appointments.inspect_billing_table(context)
            except Exception as exc:
                print(f"Inspection failed: {exc}")
            continue

        if command == "e":
            print(f"Current JSON name: {client.client}")
            corrected_name = input(
                "Type the exact CentralReach name, or press Enter to cancel: "
            ).strip()
            if not corrected_name:
                print("Name edit cancelled.")
                continue
            if corrected_name == client.client:
                print("The name was not changed.")
                continue

            corrected_client = replace(client, client=corrected_name)
            if payment_key(corrected_client) in state.get("completed", {}):
                print(
                    "That corrected name already has a completed record for this "
                    "payment and invoice. The name was not changed."
                )
                continue
            try:
                update_client_name_in_json(input_path, client, corrected_name)
                migrate_pending_state_name(state, client, corrected_client)
            except Exception as exc:
                print(f"Could not update the client name: {exc}")
                continue

            pending[current] = corrected_client
            client = corrected_client
            print(f"Saved corrected name: {client.client}")
            print("Retrying this client now without restarting the automation...")

            result = run_one(context, client, state, commit)
            reason = "" if result == "completed" else _result_reason(state, client, result)
            run_results.append(
                {"client": payment_label(client), "status": result, "reason": reason}
            )

            if result == "completed":
                pending.pop(current)
                if not pending:
                    print_run_summary(state, run_results)
                    return
                current = min(current, len(pending) - 1)
            else:
                print("\nRUN STOPPED")
                print(f"Client: {client.client}")
                print(f"Problem: {reason}")
                print("The corrected client remains pending and can be edited or retried again.")
            continue

        if command == "r":
            result = run_one(context, client, state, commit)
            reason = "" if result == "completed" else _result_reason(state, client, result)
            run_results.append({"client": payment_label(client), "status": result, "reason": reason})

            if result == "completed":
                pending.pop(current)
                if not pending:
                    print_run_summary(state, run_results)
                    return
                current = min(current, len(pending) - 1)
            else:
                print("\nRUN STOPPED")
                print(f"Client: {client.client}")
                print(f"Problem: {reason}")
                print("The client remains pending. Review the issue before running again.")
            continue

        if command == "a":
            attempted = 0
            completed_in_batch = 0
            print(f"\nStarting batch of up to {batch_size}. No further confirmation is required.")
            print("Press Ctrl+C to cancel the active client safely.")

            while attempted < batch_size and pending:
                client = pending[current]
                print_client_summary(client, current, len(pending))
                result = run_one(context, client, state, commit)
                attempted += 1
                reason = "" if result == "completed" else _result_reason(state, client, result)
                run_results.append({"client": payment_label(client), "status": result, "reason": reason})

                if result != "completed":
                    print("\nBATCH STOPPED")
                    print(f"Client: {client.client}")
                    print(f"Problem: {reason}")
                    print(
                        f"Completed before stop: {completed_in_batch}. "
                        "No later clients in this batch were processed."
                    )
                    print("The problem client remains pending for review.")
                    break

                completed_in_batch += 1
                pending.pop(current)
                if pending:
                    current = min(current, len(pending) - 1)

            if attempted == completed_in_batch:
                print(f"\nBatch complete: {completed_in_batch}/{batch_size} client(s) completed.")
            if not pending:
                print_run_summary(state, run_results)
                return
            continue

        print("Unknown command.")

