from __future__ import annotations
import logging,re
from difflib import SequenceMatcher
from typing import Callable, Optional
from playwright.sync_api import BrowserContext, Frame, Locator, Page, TimeoutError as PlaywrightTimeoutError
from config import CENTRALREACH_BILLING_URL
from browser_utils import all_frames, visible_locator, find_visible_in_frames, newest_page, screenshot, find_text_button


class ClientVerificationError(RuntimeError):
    """Raised when no CentralReach candidate matches the Excel appointments."""


def find_search_box(
    context: BrowserContext,
) -> tuple[Optional[Locator], Optional[Frame]]:
    selectors = [
        'input[placeholder*="Search billing entries" i]',
        'input[placeholder*="Search billing" i]',
        'input[placeholder*="billing entries" i]',
        'input[placeholder*="Please enter" i]',
        'input[aria-label*="Search billing" i]',
        'input[aria-label*="billing entries" i]',
        'input[type="search"]',
    ]

    return find_visible_in_frames(context, selectors)

def print_visible_inputs(context: BrowserContext) -> None:
    print("\nVisible inputs found:")

    for page_number, page in enumerate(context.pages):
        if page.is_closed():
            continue

        print(f"\nPage {page_number}: {page.url}")

        for frame_number, frame in enumerate(page.frames):
            inputs = frame.locator("input:visible")
            count = inputs.count()

            if count == 0:
                continue

            print(f"  Frame {frame_number}: {frame.url}")

            for input_number in range(min(count, 30)):
                current = inputs.nth(input_number)

                try:
                    details = {
                        "index": input_number,
                        "placeholder": current.get_attribute("placeholder"),
                        "type": current.get_attribute("type"),
                        "name": current.get_attribute("name"),
                        "aria-label": current.get_attribute("aria-label"),
                        "id": current.get_attribute("id"),
                    }
                    print("   ", details)
                except Exception:
                    continue

def wait_for_user_login(context: BrowserContext) -> Page:
    """Wait for one user confirmation after login and Billing setup."""
    page = newest_page(context)

    if not page.url or page.url == "about:blank":
        page.goto(CENTRALREACH_BILLING_URL, wait_until="domcontentloaded")

    input(
        "\nLog in to CentralReach, complete MFA, and open the Billing page. "
        "When Billing is ready, press Enter to open the command menu: "
    )

    search_box, _ = find_search_box(context)
    if search_box is not None:
        page = newest_page(context)
        logging.info("Billing page confirmed by user: %s", page.url)
        return page

    deadline = time.time() + 30
    while time.time() < deadline:
        search_box, _ = find_search_box(context)
        if search_box is not None:
            page = newest_page(context)
            logging.info("Billing page detected after startup confirmation: %s", page.url)
            return page
        newest_page(context).wait_for_timeout(500)

    raise RuntimeError(
        "Billing was not ready after the startup confirmation. Open the "
        "CentralReach Billing page and restart the script."
    )


def ensure_billing_page(context: BrowserContext) -> Page:
    """Return to Billing automatically or stop with a clear error."""
    page = newest_page(context)
    search_box, _ = find_search_box(context)

    if search_box is not None:
        return page

    back_patterns = [
        re.compile(r"Back to Billing", re.IGNORECASE),
        re.compile(r"Return to Billing", re.IGNORECASE),
    ]

    for frame in all_frames(context):
        for pattern in back_patterns:
            back_button = visible_locator(frame.get_by_text(pattern))
            if back_button is not None:
                logging.info("Returning to Billing page.")
                back_button.click()
                page.wait_for_timeout(2000)
                search_box, _ = find_search_box(context)
                if search_box is not None:
                    return newest_page(context)

    # Last automatic attempt: navigate directly to the Billing route.
    page.goto(CENTRALREACH_BILLING_URL, wait_until="domcontentloaded")
    deadline = time.time() + 15
    while time.time() < deadline:
        search_box, _ = find_search_box(context)
        if search_box is not None:
            return newest_page(context)
        newest_page(context).wait_for_timeout(500)

    print_visible_inputs(context)
    screenshot(page, "billing_search_not_found")
    raise RuntimeError(
        "Could not return to the CentralReach Billing page automatically. "
        "Automation stopped before processing another client."
    )

def strip_client_annotations(value: str) -> str:
    """Remove CentralReach status/date annotations from a displayed name."""
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", value.strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def normalize_person_name(value: str) -> list[str]:
    """Return uppercase alphanumeric name tokens for safe comparisons."""
    return re.findall(r"[A-Z0-9]+", strip_client_annotations(value).upper())


def client_name_matches(requested_name: str, result_name: str) -> bool:
    """
    Match the requested client to a CentralReach result.

    CentralReach often inserts a middle name or middle initial even when the
    payment file contains only first and last name. We allow extra tokens only
    between the requested first and last tokens. This keeps BRIAN JUSTO matched
    to BRIAN C. JUSTO without accepting a different first or last name.
    """
    requested = normalize_person_name(requested_name)
    result = normalize_person_name(result_name)

    if not requested or not result:
        return False

    if len(requested) == 1:
        return requested == result

    if requested[0] != result[0] or requested[-1] != result[-1]:
        return False

    # Preserve any requested middle-name tokens in order while permitting CR
    # to include additional middle names or initials.
    requested_middle = requested[1:-1]
    result_middle = result[1:-1]
    position = 0
    for token in requested_middle:
        try:
            position = result_middle.index(token, position) + 1
        except ValueError:
            return False
    return True


def extract_client_result(text: str) -> Optional[tuple[str, str]]:
    """Extract the displayed client name and numeric Client ID."""
    match = re.search(
        r"Client:\s*(.*?)\s*\(Client ID:\s*(\d+)\)",
        " ".join(text.split()),
        re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1).strip(), match.group(2)


def client_name_score(requested_name: str, result_name: str) -> float:
    """Rank a CentralReach result using conservative fuzzy-name intuition."""
    requested = normalize_person_name(requested_name)
    result = normalize_person_name(result_name)
    if not requested or not result:
        return 0.0

    requested_full = " ".join(requested)
    result_full = " ".join(result)
    score = SequenceMatcher(None, requested_full, result_full).ratio() * 45.0

    # First and last names carry most of the weight. Minor typos, middle
    # initials, and reversed display names can still remain candidates.
    first_ratio = SequenceMatcher(None, requested[0], result[0]).ratio()
    last_ratio = SequenceMatcher(None, requested[-1], result[-1]).ratio()
    score += first_ratio * 25.0 + last_ratio * 35.0

    if requested[0] == result[0]:
        score += 12.0
    if requested[-1] == result[-1]:
        score += 18.0
    if client_name_matches(requested_name, result_name):
        score += 30.0

    requested_set = set(requested)
    result_set = set(result)
    if requested_set and result_set:
        score += 15.0 * len(requested_set & result_set) / len(requested_set | result_set)

    return round(score, 3)


def collect_client_results(
    context: BrowserContext,
    client_name: str,
) -> list[dict[str, object]]:
    """Collect and rank visible client results, deduplicated by Client ID."""
    found: dict[str, dict[str, object]] = {}
    for frame in all_frames(context):
        candidates = frame.locator(
            "li.select2-result:visible, "
            "[role='option']:visible, "
            ".select2-result-label:visible"
        )
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            try:
                text = candidate.inner_text(timeout=1000)
            except Exception:
                continue
            parsed = extract_client_result(text)
            if parsed is None:
                continue
            displayed_name, client_id = parsed
            score = client_name_score(client_name, displayed_name)
            # Keep broad but reasonable candidates. Appointment verification is
            # the final authority, so near matches are allowed here.
            if score < 45.0:
                continue
            previous = found.get(client_id)
            item = {
                "displayed_name": displayed_name,
                "client_id": client_id,
                "score": score,
                "text": " ".join(text.split()),
            }
            if previous is None or float(previous["score"]) < score:
                found[client_id] = item
    return sorted(found.values(), key=lambda item: float(item["score"]), reverse=True)


def find_client_result_by_id(
    context: BrowserContext,
    client_id: str,
) -> Optional[Locator]:
    for frame in all_frames(context):
        candidates = frame.locator(
            "li.select2-result:visible, "
            "[role='option']:visible, "
            ".select2-result-label:visible"
        )
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            try:
                parsed = extract_client_result(candidate.inner_text(timeout=1000))
            except Exception:
                continue
            if parsed is not None and parsed[1] == str(client_id):
                return candidate
    return None


def find_client_result(
    context: BrowserContext,
    client_name: str,
) -> Optional[Locator]:
    """Return the highest-ranked visible client result for compatibility."""
    ranked = collect_client_results(context, client_name)
    if not ranked:
        return None
    return find_client_result_by_id(context, str(ranked[0]["client_id"]))

def type_into_search_box(
    context: BrowserContext,
    client_name: str,
) -> None:
    """
    Enter a client name without holding a Locator to CentralReach's search box.

    CentralReach repeatedly destroys and recreates the search input while its
    search panel updates. This function performs each action atomically inside
    the browser so a stale Locator is never reused.
    """

    page = newest_page(context)

    search_selectors = [
        'input[placeholder*="Search billing entries" i]',
        'input[placeholder*="Search billing" i]',
        'input[placeholder*="billing entries" i]',
        'input[placeholder*="Please enter 3 more characters" i]',
        'input[aria-label*="Search billing" i]',
        'input[aria-label*="billing entries" i]',
        'input[type="search"]',
    ]

    def find_frame_with_search() -> Optional[Frame]:
        for frame in all_frames(context):
            try:
                found = frame.evaluate(
                    """(selectors) => selectors.some((selector) => {
                        const elements = Array.from(
                            document.querySelectorAll(selector)
                        );
                        return elements.some((element) => {
                            const style = getComputedStyle(element);
                            const rect = element.getBoundingClientRect();
                            return (
                                style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                rect.width > 0 &&
                                rect.height > 0
                            );
                        });
                    })""",
                    search_selectors,
                )
                if found:
                    return frame
            except Exception:
                continue
        return None

    frame = find_frame_with_search()

    if frame is None:
        raise RuntimeError(
            "The CentralReach billing search input could not be found."
        )

    result = frame.evaluate(
        """({selectors, value}) => {
            function visible(element) {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            }

            let input = null;

            for (const selector of selectors) {
                const candidates = Array.from(
                    document.querySelectorAll(selector)
                );
                input = candidates.find(visible) || null;
                if (input) break;
            }

            if (!input) {
                return {
                    ok: false,
                    reason: 'not-found'
                };
            }

            input.scrollIntoView({
                block: 'center',
                inline: 'nearest'
            });
            input.focus();

            const current = String(input.value || '').trim();

            if (current.toUpperCase() === value.trim().toUpperCase()) {
                return {
                    ok: true,
                    value: input.value,
                    alreadyPresent: true
                };
            }

            const setter = Object.getOwnPropertyDescriptor(
                HTMLInputElement.prototype,
                'value'
            ).set;

            setter.call(input, '');

            input.dispatchEvent(
                new InputEvent('input', {
                    bubbles: true,
                    inputType: 'deleteContentBackward',
                    data: null
                })
            );

            setter.call(input, value);

            input.dispatchEvent(
                new InputEvent('input', {
                    bubbles: true,
                    inputType: 'insertText',
                    data: value
                })
            );

            input.dispatchEvent(
                new Event('change', {
                    bubbles: true
                })
            );

            input.dispatchEvent(
                new KeyboardEvent('keyup', {
                    bubbles: true,
                    key: value.slice(-1) || ''
                })
            );

            return {
                ok: true,
                value: input.value,
                alreadyPresent: false
            };
        }""",
        {
            "selectors": search_selectors,
            "value": client_name,
        },
    )

    logging.info("Atomic search input result: %r", result)

    if not result or not result.get("ok"):
        raise RuntimeError(
            "The CentralReach billing search input was found earlier, "
            "but could not be updated."
        )

    page.wait_for_timeout(1800)

    # Verify using a fresh atomic DOM query. Do not reuse the old element.
    frame = find_frame_with_search()

    if frame is None:
        # The input may briefly disappear while results render. This is okay as
        # long as the matching client result is visible.
        if find_client_result(context, client_name) is not None:
            logging.info(
                "Search input disappeared after typing, but the client result is visible."
            )
            return

        raise RuntimeError(
            "The search input disappeared and no matching client result appeared."
        )

    verification = frame.evaluate(
        """({selectors, expected}) => {
            function visible(element) {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            }

            for (const selector of selectors) {
                const candidate = Array.from(
                    document.querySelectorAll(selector)
                ).find(visible);

                if (candidate) {
                    return String(candidate.value || '');
                }
            }

            return null;
        }""",
        {
            "selectors": search_selectors,
            "expected": client_name,
        },
    )

    logging.info("Search value after atomic entry: %r", verification)

    if (
        verification is not None
        and str(verification).strip().upper()
        == client_name.strip().upper()
    ):
        return

    if find_client_result(context, client_name) is not None:
        logging.info(
            "The input value changed during rendering, but the exact client result is visible."
        )
        return

    screenshot(page, f"typing_failed_{client_name}")
    raise RuntimeError(
        "CentralReach did not retain the client name and no exact client result appeared."
    )

def search_client(
    context: BrowserContext,
    client_name: str,
    verifier: Optional[Callable[[], tuple[bool, str]]] = None,
) -> Page:
    """
    Search only the complete Excel client name and verify visible options.

    CentralReach may append status or birth-date annotations to a result. Those
    annotations are ignored for ranking, but the script never broadens the
    query to first/last name or surname-only searches.
    """
    logging.info("Searching client: %s", client_name)
    page = ensure_billing_page(context)
    full_name = " ".join(normalize_person_name(client_name)).strip() or client_name.strip()

    type_into_search_box(context, full_name)
    page.wait_for_timeout(2200)
    ranked = [
        item for item in collect_client_results(context, client_name)
        if float(item["score"]) >= 130.0
    ]

    if not ranked:
        screenshot(page, f"client_result_not_found_{client_name}")
        raise ClientVerificationError(
            f"No client options were found from the full-name search for {client_name}."
        )

    logging.info(
        "Ranked full-name candidates for %s: %s",
        client_name,
        [(item["displayed_name"], item["client_id"], item["score"]) for item in ranked],
    )
    failures: list[str] = []

    for position, item in enumerate(ranked, 1):
        if position > 1:
            return_to_billing(context)
            type_into_search_box(context, full_name)
            page.wait_for_timeout(1800)

        result = find_client_result_by_id(context, str(item["client_id"]))
        if result is None:
            failures.append(f"{item['displayed_name']}: result disappeared")
            continue

        logging.info(
            "Trying full-name candidate %d/%d for %s: %s (Client ID %s, score %.1f)",
            position,
            len(ranked),
            client_name,
            item["displayed_name"],
            item["client_id"],
            float(item["score"]),
        )
        result.scroll_into_view_if_needed()
        try:
            result.click(timeout=10_000)
        except Exception:
            result.click(force=True, timeout=10_000)
        page.wait_for_timeout(2500)

        if verifier is None:
            return newest_page(context)

        try:
            verified, reason = verifier()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            verified, reason = False, f"verification error: {exc}"
        if verified:
            logging.info(
                "Verified %s as %s using appointment data.",
                client_name,
                item["displayed_name"],
            )
            return newest_page(context)

        failure = f"{item['displayed_name']} (ID {item['client_id']}): {reason}"
        failures.append(failure)
        logging.warning("Rejected candidate for %s: %s", client_name, failure)

    screenshot(newest_page(context), f"no_verified_client_{client_name}")
    raise ClientVerificationError(
        f"None of the options returned by the full-name search for {client_name} "
        f"matched the Excel appointments. Tried: {'; '.join(failures)}"
    )

def return_to_billing(context: BrowserContext) -> None:
    """Return to Billing automatically without pausing for console input."""
    page = newest_page(context)

    search_box, _ = find_search_box(context)
    if search_box is not None:
        return

    patterns = [
        re.compile(r"Back to Billing", re.IGNORECASE),
        re.compile(r"Return to Billing", re.IGNORECASE),
        re.compile(r"Cancel", re.IGNORECASE),
    ]
    for pattern in patterns:
        button = find_text_button(context, pattern)
        if button is None:
            continue
        try:
            button.click(timeout=5000)
        except Exception:
            try:
                button.click(force=True, timeout=5000)
            except Exception:
                continue
        page.wait_for_timeout(1800)
        search_box, _ = find_search_box(context)
        if search_box is not None:
            return

    # Use browser history before falling back to a direct Billing URL. This
    # usually preserves the user's date and code-label filters.
    try:
        page.go_back(wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(1800)
    except Exception:
        pass

    search_box, _ = find_search_box(context)
    if search_box is not None:
        return

    try:
        page.goto(CENTRALREACH_BILLING_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2200)
    except Exception as exc:
        raise RuntimeError(
            "Could not return to the CentralReach Billing page automatically."
        ) from exc

    search_box, _ = find_search_box(context)
    if search_box is None:
        screenshot(page, "automatic_return_to_billing_failed")
        raise RuntimeError(
            "Could not return to the CentralReach Billing page automatically. "
            "No manual Enter prompt was used."
        )

