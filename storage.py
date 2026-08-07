from __future__ import annotations

import csv
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Any

from config import (
    DEFAULT_LOG,
    DEFAULT_STATE,
    PROCESSED_LOG,
    RUN_REPORT,
)
from models import AppointmentPayment, ClientPayment


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[
            RotatingFileHandler(
                DEFAULT_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def load_clients(path: Path) -> list[ClientPayment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("validation_issues"):
        raise RuntimeError("The input JSON contains validation issues.")
    clients: list[ClientPayment] = []
    for item in data.get("clients", []):
        raw = item.get("appointments") or []
        if not raw:
            raise RuntimeError(
                f"{item.get('client')}: appointment-level Submitted_amount data is missing."
            )
        appointments = tuple(
            AppointmentPayment(
                date=str(a["date"]),
                submitted_amount=round(float(a["submitted_amount"]), 2),
                cpt_code=str(a.get("cpt_code", "")),
                staff_name=str(a.get("staff_name", "")),
                pos=str(a.get("pos", "")),
            )
            for a in raw
        )
        total = round(sum(a.submitted_amount for a in appointments), 2)
        if len(appointments) != int(item["total_appointments"]):
            raise ValueError(f"{item['client']}: appointment count mismatch in JSON.")
        if abs(total - round(float(item.get("expected_total", total)), 2)) > 0.01:
            raise ValueError(f"{item['client']}: expected_total mismatch in JSON.")
        clients.append(
            ClientPayment(
                str(item["client"]).strip(),
                str(item["payment_date"]),
                str(item["payment_number"]).strip(),
                str(item["invoice_number"]).strip(),
                len(appointments),
                total,
                appointments,
            )
        )
    return clients



def update_client_name_in_json(path: Path, client: ClientPayment, new_name: str) -> None:
    """Persist a corrected client name for one payment run.

    The payment number and invoice number identify the exact JSON entry so a name
    can be corrected while the browser session remains open.
    """
    corrected_name = new_name.strip()
    if not corrected_name:
        raise ValueError("Client name cannot be blank.")

    data = json.loads(path.read_text(encoding="utf-8"))
    matches = [
        item
        for item in data.get("clients", [])
        if str(item.get("client", "")).strip() == client.client
        and str(item.get("payment_number", "")).strip() == client.payment_number
        and str(item.get("invoice_number", "")).strip() == client.invoice_number
    ]
    if not matches:
        raise ValueError(
            "Could not find this client/payment/invoice entry in the input JSON."
        )
    if len(matches) > 1:
        raise ValueError(
            "Multiple matching JSON entries were found. The name was not changed."
        )

    matches[0]["client"] = corrected_name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def migrate_pending_state_name(
    state: dict[str, Any], old_client: ClientPayment, new_client: ClientPayment
) -> None:
    """Move non-completed status records to the corrected payment key."""
    old_key = payment_key(old_client)
    new_key = payment_key(new_client)
    if new_key in state.get("completed", {}):
        raise ValueError(
            "The corrected name already has a completed record for this payment and invoice."
        )

    for section in ("blocked", "failed", "skipped"):
        records = state.setdefault(section, {})
        record = records.pop(old_key, None)
        if record is not None:
            record["client"] = new_client.client
            records[new_key] = record
    save_state(state)


def payment_key(client: ClientPayment) -> str:
    """Unique progress key for one client/payment/invoice run."""
    return f"{client.client} | {client.payment_number} | {client.invoice_number}"


def payment_label(client: ClientPayment) -> str:
    return f"{client.client} [{client.invoice_number} / {client.payment_date}]"

def load_state(path: Path = DEFAULT_STATE) -> dict[str, Any]:
    state: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {"completed": {}, "failed": {}, "skipped": {}}
    )
    state.setdefault("completed", {})
    state.setdefault("failed", {})
    state.setdefault("skipped", {})
    state.setdefault("blocked", {})
    # Recover completed payment runs from the archive even if progress.json was lost.
    if PROCESSED_LOG.exists():
        try:
            archive = json.loads(PROCESSED_LOG.read_text(encoding="utf-8"))
            for item in archive.get("clients", []):
                name = str(item.get("client", "")).strip()
                payment_number = str(item.get("payment_number", "")).strip()
                invoice_number = str(item.get("invoice_number", "")).strip()
                if name and payment_number and invoice_number:
                    key = f"{name} | {payment_number} | {invoice_number}"
                    state["completed"].setdefault(
                        key,
                        {
                            "client": name,
                            "timestamp": item.get("processed_timestamp", "archive"),
                            "appointments": item.get("total_appointments"),
                            "payment_number": payment_number,
                            "invoice_number": invoice_number,
                            "payment_date": item.get("payment_date"),
                            "expected_total": item.get("expected_total"),
                        },
                    )
        except Exception as exc:
            logging.warning("Could not import processed_clients.json: %s", exc)
    return state


def save_state(state: dict[str, Any], path: Path = DEFAULT_STATE) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def archive_completed(client: ClientPayment) -> None:
    data = (
        json.loads(PROCESSED_LOG.read_text(encoding="utf-8"))
        if PROCESSED_LOG.exists()
        else {"clients": []}
    )
    existing = {
        (
            str(item.get("client", "")).strip(),
            str(item.get("payment_number", "")).strip(),
            str(item.get("invoice_number", "")).strip(),
        )
        for item in data.get("clients", [])
    }
    key = (client.client, client.payment_number, client.invoice_number)
    if key in existing:
        return
    data.setdefault("clients", []).append(
        {
            "client": client.client,
            "payment_date": client.payment_date,
            "payment_number": client.payment_number,
            "invoice_number": client.invoice_number,
            "total_appointments": client.total_appointments,
            "expected_total": client.expected_total,
            "processed_timestamp": datetime.now().isoformat(),
        }
    )
    PROCESSED_LOG.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_report(client: ClientPayment, status: str, error: str = "") -> None:
    new_file = not RUN_REPORT.exists()
    with RUN_REPORT.open("a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        if new_file:
            writer.writerow(
                [
                    "timestamp",
                    "client",
                    "payment_number",
                    "invoice_number",
                    "payment_date",
                    "appointments",
                    "amount",
                    "status",
                    "error",
                ]
            )
        writer.writerow(
            [
                datetime.now().isoformat(),
                client.client,
                client.payment_number,
                client.invoice_number,
                client.payment_date,
                client.total_appointments,
                f"{client.expected_total:.2f}",
                status,
                error,
            ]
        )
