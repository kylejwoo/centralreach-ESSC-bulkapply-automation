from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import BrowserContext, Locator

from browser_utils import all_frames


LAST_TABLE_DIAGNOSTICS: dict[str, Any] = {}


def _clean(value: str) -> str:
    return " ".join(str(value).split()).strip()


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).lower()).strip()


@dataclass(frozen=True)
class BillingColumnMap:
    date_index: int
    charges_billed_index: int
    leaf_paths: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "date_index": self.date_index,
            "charges_billed_index": self.charges_billed_index,
            "leaf_paths": list(self.leaf_paths),
        }


def _extract_header_paths(table: Locator) -> dict[str, Any]:
    """Return a stable leaf-column map for CentralReach's grouped headers.

    CentralReach places banners, filter controls, and sometimes duplicate sticky
    header rows inside ``thead``. A generic rowspan/colspan grid therefore
    produces polluted paths. This routine instead finds a leaf header row whose
    cell count matches the billing body, then pairs it with the nearest preceding
    grouped header row (WORK, RATE, DRIVE, CHARGES, AMOUNTS).
    """
    return table.evaluate(
        r"""(table) => {
            const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
            const rows = Array.from(table.querySelectorAll('thead tr'));
            const bodyRow = Array.from(table.querySelectorAll('tbody tr')).find((row) => {
                const cells = Array.from(row.children).filter((c) => c.matches('td,th'));
                return cells.length >= 20;
            });
            const bodyWidth = bodyRow
                ? Array.from(bodyRow.children).filter((c) => c.matches('td,th')).length
                : 0;

            const rowInfo = rows.map((row, index) => {
                const cells = Array.from(row.children).filter((c) => c.matches('th'));
                const expandedWidth = cells.reduce(
                    (sum, cell) => sum + Math.max(1, Number(cell.getAttribute('colspan') || 1)),
                    0
                );
                return {
                    index,
                    cells,
                    cellCount: cells.length,
                    expandedWidth,
                    texts: cells.map((cell) => clean(cell.innerText || cell.textContent || '')),
                    colspans: cells.map((cell) => Math.max(1, Number(cell.getAttribute('colspan') || 1)))
                };
            });

            // The leaf row has one TH per actual billing column. Prefer a row
            // matching the first real body row and containing Date + Billed.
            let leaf = rowInfo.find((info) =>
                info.cellCount === bodyWidth &&
                info.texts.some((text) => /^Date\b/i.test(text)) &&
                info.texts.filter((text) => /^Billed\b/i.test(text)).length >= 2
            );
            if (!leaf) {
                leaf = rowInfo.find((info) =>
                    info.cellCount >= 20 &&
                    info.texts.some((text) => /^Date\b/i.test(text)) &&
                    info.texts.some((text) => /^Billed\b/i.test(text))
                );
            }
            if (!leaf) {
                return {bodyWidth, paths: [], rowInfo: rowInfo.map(({cells, ...rest}) => rest)};
            }

            // Find the closest prior grouped row that expands to the same width.
            let group = null;
            for (let i = leaf.index - 1; i >= 0; i--) {
                const candidate = rowInfo[i];
                const joined = candidate.texts.join(' ').toUpperCase();
                if (
                    candidate.expandedWidth === leaf.cellCount &&
                    /\bCHARGES\b/.test(joined) &&
                    /\bWORK\b/.test(joined)
                ) {
                    group = candidate;
                    break;
                }
            }

            const groups = Array(leaf.cellCount).fill('');
            if (group) {
                let column = 0;
                group.texts.forEach((text, i) => {
                    const span = group.colspans[i];
                    for (let j = 0; j < span && column + j < groups.length; j++) {
                        groups[column + j] = text;
                    }
                    column += span;
                });
            }

            const paths = leaf.texts.map((leafText, i) => {
                const groupText = groups[i] || '';
                return groupText ? `${groupText} > ${leafText}` : leafText;
            });
            return {
                bodyWidth,
                leafRowIndex: leaf.index,
                groupRowIndex: group ? group.index : null,
                paths,
                rowInfo: rowInfo.map(({cells, ...rest}) => rest)
            };
        }"""
    )


def detect_billing_table(context: BrowserContext) -> tuple[Locator, BillingColumnMap]:
    table_attempts: list[dict[str, Any]] = []

    for frame_index, frame in enumerate(all_frames(context)):
        tables = frame.locator("table:visible")
        for table_index in range(tables.count()):
            table = tables.nth(table_index)
            try:
                extracted = _extract_header_paths(table)
                paths = [_clean(x) for x in extracted.get("paths", [])]
            except Exception as exc:
                table_attempts.append({
                    "frame_index": frame_index,
                    "table_index": table_index,
                    "error": str(exc),
                })
                continue

            attempt = {
                "frame_index": frame_index,
                "table_index": table_index,
                **extracted,
            }
            table_attempts.append(attempt)
            normalized = [_norm(path) for path in paths]

            date_candidates = [
                i for i, path in enumerate(normalized)
                if path == "date" or path.startswith("date ") or path.endswith(" date")
            ]
            charge_candidates = [
                i for i, path in enumerate(normalized)
                if path.startswith("charges billed")
                or ("charges" in path.split() and re.search(r"\bbilled\b", path))
            ]

            if not date_candidates or not charge_candidates:
                continue
            if table.locator("tbody tr input[type='checkbox']").count() == 0:
                continue

            mapping = BillingColumnMap(
                date_index=date_candidates[-1],
                charges_billed_index=charge_candidates[0],
                leaf_paths=tuple(paths),
            )
            global LAST_TABLE_DIAGNOSTICS
            LAST_TABLE_DIAGNOSTICS = {
                "status": "detected",
                "selected_frame_index": frame_index,
                "selected_table_index": table_index,
                "column_map": mapping.as_dict(),
                "table_attempts": table_attempts,
            }
            logging.info(
                "Detected billing columns: Date=%d (%s), Charges Billed=%d (%s)",
                mapping.date_index,
                paths[mapping.date_index],
                mapping.charges_billed_index,
                paths[mapping.charges_billed_index],
            )
            return table, mapping

    LAST_TABLE_DIAGNOSTICS = {
        "status": "not_detected",
        "table_attempts": table_attempts,
    }
    raise RuntimeError(
        "Could not detect the CentralReach billing table with both Date and "
        "CHARGES > Billed columns. Parser diagnostics were saved in the debug bundle."
    )
