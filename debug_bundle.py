from __future__ import annotations

import json
import logging
import shutil
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from playwright.sync_api import BrowserContext

from browser_utils import newest_page, safe_filename
from config import (
    DEBUG_BUNDLES,
    DIAGNOSTIC_RETENTION_DAYS,
    MAX_DEBUG_BUNDLES,
    SAVE_FAILURE_DIAGNOSTICS,
    SCREENSHOTS,
)
from models import ClientPayment


def cleanup_old_diagnostics() -> None:
    """Remove legacy screenshots and limit retained failure bundles."""
    try:
        # Production mode no longer uses the legacy screenshots directory.
        if SCREENSHOTS.exists():
            shutil.rmtree(SCREENSHOTS, ignore_errors=True)

        if not DEBUG_BUNDLES.exists():
            return

        # Remove unpacked temporary folders left by interrupted older runs.
        for child in DEBUG_BUNDLES.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

        bundles = sorted(
            DEBUG_BUNDLES.glob("*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        cutoff = datetime.now() - timedelta(days=max(1, DIAGNOSTIC_RETENTION_DAYS))
        for index, path in enumerate(bundles):
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if index >= max(1, MAX_DEBUG_BUNDLES) or modified < cutoff:
                path.unlink(missing_ok=True)
    except Exception as exc:
        logging.warning("Could not clean old diagnostics: %s", exc)


def create_debug_bundle(
    context: BrowserContext,
    client: ClientPayment,
    error: BaseException,
    diagnostics: Optional[dict[str, Any]] = None,
) -> Optional[Path]:
    """Create one compressed diagnostic ZIP only when a run fails."""
    if not SAVE_FAILURE_DIAGNOSTICS:
        return None

    folder: Optional[Path] = None
    try:
        cleanup_old_diagnostics()
        DEBUG_BUNDLES.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = (
            f"debug_{stamp}_{safe_filename(client.client)}_"
            f"{safe_filename(client.invoice_number)}"
        )
        folder = DEBUG_BUNDLES / stem
        folder.mkdir(parents=True, exist_ok=True)
        page = newest_page(context)

        try:
            page.screenshot(path=str(folder / "screenshot.png"), full_page=True)
        except Exception as exc:
            (folder / "screenshot_error.txt").write_text(str(exc), encoding="utf-8")

        try:
            (folder / "page.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            (folder / "html_error.txt").write_text(str(exc), encoding="utf-8")

        expected = {
            "client": client.client,
            "payment_date": client.payment_date,
            "payment_number": client.payment_number,
            "invoice_number": client.invoice_number,
            "total_appointments": client.total_appointments,
            "expected_total": client.expected_total,
            "appointments": [
                {
                    "date": a.date,
                    "submitted_amount": a.submitted_amount,
                    "cpt_code": a.cpt_code,
                    "staff_name": a.staff_name,
                    "pos": a.pos,
                }
                for a in client.appointments
            ],
            "error_type": type(error).__name__,
            "error": str(error),
        }
        (folder / "expected.json").write_text(
            json.dumps(expected, indent=2), encoding="utf-8"
        )
        (folder / "diagnostics.json").write_text(
            json.dumps(diagnostics or {}, indent=2, default=str), encoding="utf-8"
        )

        zip_path = DEBUG_BUNDLES / f"{stem}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in folder.rglob("*"):
                if path.is_file():
                    archive.write(path, path.relative_to(folder))
        logging.info("Saved failure debug bundle: %s", zip_path)
        cleanup_old_diagnostics()
        return zip_path
    except Exception as exc:
        logging.warning("Could not create debug bundle: %s", exc)
        return None
    finally:
        if folder is not None and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
