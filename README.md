# CentralReach Payment Automation

A Playwright-based automation tool that applies bulk electronic payments to
clients in [CentralReach](https://centralreach.com/) based on an input file of
expected payments and appointments. Built with a "stop on uncertainty" safety
philosophy: the automation halts and explains itself rather than guessing
whenever a client, appointment, or total can't be confidently verified.

> **This is an internal/personal automation tool.** It drives a real browser
> against a live CentralReach account and can submit real payments. Read the
> [Safety model](#safety-model) section before running it, and always start
> in dry-run mode.

## How it works

1. You provide a JSON file describing expected payments: for each client, a
   payment date, payment/invoice numbers, and a list of appointments (date +
   submitted amount).
2. The script launches a real Chromium browser (via Playwright) using a
   persistent profile, so you log in and complete MFA once per session.
3. For each client, it:
   - Searches CentralReach by full name and verifies the result by matching
     the client's actual billing appointments against the input file.
   - Selects the matching appointment checkboxes in the billing table.
   - Opens **Bulk Apply Payments**, fills in Date / Payment Type / Reference /
     Notes, and verifies CentralReach's own Grand Total matches the expected
     total.
   - Submits the payment (only in `--commit` mode) and confirms success.
4. Progress is tracked per `client | payment_number | invoice_number` so the
   same client can appear in multiple payment runs (e.g. two separate
   invoices) without one being skipped because of the other.

## Project structure

```
centralreach_payments.py   CLI entry point
config.py                  Paths and tunable settings
models.py                  ClientPayment / AppointmentPayment dataclasses
browser_utils.py           Low-level Playwright helpers (frames, locators, zoom)
navigation.py              Client search, fuzzy name matching, Billing page recovery
table_parser.py            Detects the Date / Charges-Billed columns in CentralReach's table
appointments.py            Reads billing rows, verifies/selects matching appointments
payments.py                Bulk Apply Payments flow: fill form, verify totals, submit
workflow.py                Interactive command loop and run/batch orchestration
storage.py                 JSON/CSV persistence: clients, progress state, reports
debug_bundle.py            Zips diagnostics (screenshot, HTML, expected data) on failure
examples/sample_input.json Sanitized example input file (no real client data)
```

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Input file format

See [`examples/sample_input.json`](examples/sample_input.json) for the shape
CentralReach expects:

```json
{
  "clients": [
    {
      "client": "JANE DOE",
      "payment_number": "EFT000000000001",
      "invoice_number": "PROGR001-000001",
      "payment_date": "2026-01-15",
      "appointments": [
        {"date": "1/6/2026", "submitted_amount": 93.5, "cpt_code": "92507", "staff_name": "SAMPLE STAFF", "pos": ""}
      ],
      "total_appointments": 1,
      "expected_total": 93.5
    }
  ]
}
```

Your real input file will contain actual client names and payment data — keep
it **outside** this repo (it's already excluded via `.gitignore`) and pass its
path with `--input`.

## Usage

Dry run (default — never clicks the final submit button):

```bash
python centralreach_payments.py --input /path/to/your_input.json
```

Commit mode (submits real payments):

```bash
python centralreach_payments.py --input /path/to/your_input.json --commit
```

Resume from a specific client:

```bash
python centralreach_payments.py --start-client "JANE DOE | PROGR001-000001"
```

### Interactive commands

Once the Billing page is detected, you'll get a menu:

| Key | Action |
|-----|--------|
| `R` | Run the currently displayed client |
| `A` | Run a batch (default 5, change with `S`) |
| `E` | Edit a client's name in the JSON and retry immediately |
| `S` | Change batch size |
| `I` | Inspect the detected billing table columns |
| `N` / `P` | Next / previous client |
| `C` | Choose a client by name |
| `B` | Return to Billing |
| `Q` | Quit and print a run summary |

A batch stops immediately on any uncertain, blocked, or failed client so
nothing after it is touched until you've reviewed the problem.

## Safety model

- **Full-name search only** — never falls back to surname-only search.
- **Appointment verification before payment** — a candidate client is only
  accepted if their real CentralReach appointments match the input file's
  dates and amounts exactly (within $0.01).
- **Grand Total cross-check** — CentralReach's own bulk-payment Grand Total
  must match the expected total before submission.
- **Stop on uncertainty** — any mismatch, ambiguous search result, or
  unclear submission confirmation halts that client (and, in batch mode, the
  batch) rather than guessing.
- **Dry-run by default** — `--commit` must be passed explicitly to submit
  real payments.

## Diagnostics

- `automation.log` — rotating log (5 MB, 3 backups).
- `debug_bundles/` — a ZIP per failure (screenshot, page HTML, expected data,
  parser diagnostics), auto-pruned after `DIAGNOSTIC_RETENTION_DAYS` days or
  `MAX_DEBUG_BUNDLES` files (see `config.py`).
- `run_summary.csv` — one row per processed client per run.
- `progress.json` / `processed_clients.json` — persisted completion state.

Set `DEBUG_MODE = True` in `config.py` to enable routine screenshots during
development.

## Configuration

All tunables live in [`config.py`](config.py), including:

- `CENTRALREACH_BILLING_URL` — login/billing entry URL
- `PAYMENT_TYPE_OPTION` — the Payment Type to select (default `"Electronic"`)
- `PAGE_ZOOM_PERCENT` — browser zoom so wide payment tables fit on screen
- `DEBUG_MODE`, `SAVE_FAILURE_DIAGNOSTICS`, `DIAGNOSTIC_RETENTION_DAYS`,
  `MAX_DEBUG_BUNDLES`

## Requirements

- Python 3.9+
- [Playwright](https://playwright.dev/python/) (`pip install playwright`)

## Disclaimer

This tool interacts with a live third-party web application (CentralReach)
via browser automation, not a public API. Selectors and page structure may
change without notice, which can break this automation; always verify in
dry-run mode after a CentralReach UI update before using `--commit`. Use of
this tool is your own responsibility, including compliance with your
organization's data-handling and payment-authorization policies.

## License

MIT — see [LICENSE](LICENSE).
