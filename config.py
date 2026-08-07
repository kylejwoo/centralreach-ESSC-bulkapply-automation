from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "CentralReach_Payment_Automation_Input_with_amounts.json"
DEFAULT_PROFILE = ROOT / "browser_profile"
DEFAULT_STATE = ROOT / "progress.json"
DEFAULT_LOG = ROOT / "automation.log"
PROCESSED_LOG = ROOT / "processed_clients.json"
RUN_REPORT = ROOT / "run_summary.csv"
SCREENSHOTS = ROOT / "screenshots"
DEBUG_BUNDLES = ROOT / "debug_bundles"
CENTRALREACH_BILLING_URL = "https://login.centralreach.com/login?ReturnUrl=%2F"
PAYMENT_TYPE_OPTION = "Electronic"
REQUIRE_MANUAL_CONFIRMATION_BEFORE_COMMIT = False
MANUALLY_COMPLETED_CLIENTS = set()

# Zoom CentralReach out slightly so wide payment tables fit on screen.
PAGE_ZOOM_PERCENT = 80

# Production diagnostics settings.
# Routine screenshots and HTML dumps are disabled to prevent disk growth.
DEBUG_MODE = False
SAVE_FAILURE_DIAGNOSTICS = True
DIAGNOSTIC_RETENTION_DAYS = 30
MAX_DEBUG_BUNDLES = 20
