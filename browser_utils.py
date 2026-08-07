from __future__ import annotations
import logging,re
from datetime import datetime
from pathlib import Path
from typing import Optional
from playwright.sync_api import BrowserContext, Frame, Locator, Page, Playwright
from config import DEFAULT_PROFILE, PAGE_ZOOM_PERCENT, SCREENSHOTS, DEBUG_MODE

def safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)

def screenshot(page: Page, label: str, *, force: bool = False) -> None:
    """Save a screenshot only in debug mode or when explicitly forced for a failure."""
    if not (DEBUG_MODE or force):
        return
    try:
        SCREENSHOTS.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOTS / f"{timestamp}_{safe_filename(label)}.png"
        page.screenshot(path=str(path), full_page=True)
        logging.info("Saved screenshot: %s", path)
    except Exception as exc:
        logging.warning("Could not save screenshot: %s", exc)

def newest_page(context: BrowserContext) -> Page:
    open_pages = [page for page in context.pages if not page.is_closed()]

    if not open_pages:
        return context.new_page()

    return open_pages[-1]

def all_frames(context: BrowserContext) -> list[Frame]:
    frames: list[Frame] = []

    for page in context.pages:
        if page.is_closed():
            continue
        frames.extend(page.frames)

    return frames

def visible_locator(locator: Locator) -> Optional[Locator]:
    try:
        count = locator.count()
    except Exception:
        return None

    for index in range(count):
        candidate = locator.nth(index)

        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue

    return None

def find_visible_in_frames(
    context: BrowserContext,
    selectors: list[str],
) -> tuple[Optional[Locator], Optional[Frame]]:
    for frame in all_frames(context):
        for selector in selectors:
            candidate = visible_locator(frame.locator(selector))

            if candidate is not None:
                logging.info(
                    "Found element with selector %s in frame %s",
                    selector,
                    frame.url,
                )
                return candidate, frame

    return None, None

def find_text_button(
    context: BrowserContext,
    pattern: re.Pattern[str],
) -> Optional[Locator]:
    for frame in all_frames(context):
        button = visible_locator(frame.get_by_role("button", name=pattern))

        if button is not None:
            return button

        text = visible_locator(frame.get_by_text(pattern))

        if text is not None:
            return text

    return None

def launch_context(playwright: Playwright) -> BrowserContext:
    # Do not force a fixed 1600x1000 viewport. On smaller displays that made
    # the right and bottom edges extend beyond the visible browser window.
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(DEFAULT_PROFILE),
        headless=False,
        no_viewport=True,
        slow_mo=150,
        args=[
            "--start-maximized",
            "--window-position=0,0",
        ],
    )

    # CentralReach tables are very wide. Apply a browser-page zoom in every
    # document and iframe so the payment columns and bottom controls fit on
    # screen without changing any selectors or payment logic.
    zoom = max(50, min(100, int(PAGE_ZOOM_PERCENT))) / 100
    context.add_init_script(
        f"""
        (() => {{
            const applyZoom = () => {{
                document.documentElement.style.zoom = '{zoom:.2f}';
            }};
            if (document.readyState === 'loading') {{
                document.addEventListener('DOMContentLoaded', applyZoom, {{ once: true }});
            }} else {{
                applyZoom();
            }}
        }})();
        """
    )

    # The first page may already exist before add_init_script is registered.
    # Apply the same zoom immediately to any open page/frame as well.
    for page in context.pages:
        for frame in page.frames:
            try:
                frame.evaluate(
                    "value => { document.documentElement.style.zoom = value; }",
                    f"{zoom:.2f}",
                )
            except Exception:
                pass

    return context
