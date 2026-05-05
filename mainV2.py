"""
book_slots_full.py (multi‑date search – master copy)

Requirements:
    pip install playwright==1.37 python-dotenv beautifulsoup4 lxml
    playwright install chromium
"""

from __future__ import annotations

import asyncio
import os
import re
import smtplib
import ssl
import unicodedata
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import List, Set

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page

# ───────────────────────────
#  Config
# ───────────────────────────
ENV_PATH = Path(__file__).with_name("variables.env")
if ENV_PATH.exists():
    # Keep CI-provided environment variables intact and only use the file as a local fallback.
    load_dotenv(ENV_PATH, override=False)

EMAIL    = os.getenv("LOGIN_EMAIL")
PASSWORD = os.getenv("LOGIN_PASSWORD")
BASE     = "https://ravenair.volanti.club"

TYPE_VAL   = "5"           # Training (Dual)
COURSE_VAL = "3"           # UK PPL – PA28

DATE_WINDOW_DAYS = int(os.getenv("DATE_WINDOW_DAYS", "57"))      # inclusive
DATE_START_OFFSET_DAYS = int(os.getenv("DATE_START_OFFSET_DAYS", "2"))
SEARCH_WAIT_MS   = int(os.getenv("SEARCH_WAIT_MS",   "2500"))    # ms after clicking Search
SEARCH_ATTEMPTS  = int(os.getenv("SEARCH_ATTEMPTS",  "2"))       # retries per date (was 3)

SNAP_PATH = Path("slots_latest.txt")          # snapshot file (UTF‑8)
SMTP = {k: os.getenv(k) for k in (
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_TO"
)}
VERBOSE = bool(int(os.getenv("VERBOSE", "0")))

# ───────────────────────────
#  Canonical helpers
# ───────────────────────────
_WS_RE = re.compile(r"\s+")
_TIME_RE = re.compile(r"(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})")


def _canonical(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = s.replace("–", "-").replace("—", "-")
    s = _WS_RE.sub(" ", s)
    return s.strip()


def _normalise_date(raw: str) -> str:
    return datetime.strptime(raw.strip(), "%B %d, %Y").strftime("%Y-%m-%d")


def _card_to_line(card_div) -> str | None:
    date_span = card_div.select_one("span.text-gray-400.text-sm")
    time_span = card_div.select_one("span.font-semibold.text-xl")
    if not date_span or not time_span:
        return None

    m = _TIME_RE.search(time_span.get_text())
    if not m:
        return None
    start, end = m.groups()

    date_iso = _normalise_date(date_span.get_text())
    title    = (card_div.find_previous("h3") or "").get_text(strip=True) or "Unknown booking"
    return _canonical(f"{date_iso} {start}-{end} - {title}")


def html_to_lines(html: str) -> List[str]:
    soup  = BeautifulSoup(html, "lxml")
    cards = soup.select("div.p-6.flex")
    lines = filter(None, (_card_to_line(c) for c in cards))
    return sorted(set(lines))


def _vprint(*parts: object) -> None:
    print(*parts, flush=True)


async def _dump_first_day_page(page: Page, iso_date: str) -> None:
    _vprint("[dump] first-day target:", iso_date)
    _vprint("[dump] url:", page.url)
    _vprint("[dump] title:", await page.title())

    date_value = ""
    date_text = ""
    date_select = page.locator('select#form\\.date')
    if await date_select.count():
        date_value = await date_select.input_value()
        date_text = await date_select.locator("option:checked").inner_text()
    _vprint("[dump] selected date value:", repr(date_value))
    _vprint("[dump] selected date text:", repr(_WS_RE.sub(" ", date_text).strip()))

    html = await page.content()
    body_text = _WS_RE.sub(" ", await page.locator("body").inner_text()).strip()

    _vprint("[dump] page contains target iso date:", iso_date in html)
    _vprint("[dump] page contains changeDate target:", f"changeDate('{iso_date}')" in html)
    _vprint("[dump] page contains booking results cards:", 'div class="p-6 flex flex-row justify-between"' in html)
    _vprint("[dump] body text snippet:")
    _vprint(body_text[:2000])
    _vprint("[dump] html snippet:")
    _vprint(html[:4000])

# ───────────────────────────
#  Playwright helpers
# ───────────────────────────
async def _login(page: Page) -> None:
    if not EMAIL or not PASSWORD:
        raise RuntimeError(
            "Missing LOGIN_EMAIL or LOGIN_PASSWORD. "
            f"Set them in the environment or add them to {ENV_PATH} for local testing."
        )
    _vprint("[login] opening", f"{BASE}/auth/login")
    await page.goto(f"{BASE}/auth/login")
    _vprint("[login] landed on", page.url)
    if page.url.startswith(f"{BASE}/me"):
        _vprint("[login] session already active")
        return
    _vprint("[login] filling email")
    await page.fill('input[name="email"]', EMAIL)
    _vprint("[login] filling password")
    await page.fill('input[name="password"]', PASSWORD)
    _vprint("[login] clicking submit")
    await page.click('button[type="submit"]')
    _vprint("[login] waiting for account page")
    await page.wait_for_url(f"{BASE}/me")
    _vprint("[login] complete", page.url)


async def _prepare_search(page: Page) -> None:
    _vprint("[search] opening", f"{BASE}/bookings/search")
    await page.goto(f"{BASE}/bookings/search")
    _vprint("[search] landed on", page.url)
    _vprint("[search] selecting type", TYPE_VAL)
    await page.locator('select#form\\.type').select_option(TYPE_VAL)
    _vprint("[search] waiting for course selector")
    await page.wait_for_selector('select#form\\.course', state="attached", timeout=10_000)
    _vprint("[search] selecting course", COURSE_VAL)
    await page.locator('select#form\\.course').select_option(COURSE_VAL)


async def _select_date_safe(page: Page, iso_date: str) -> bool:
    opt_sel = f'select#form\\.date option[value="{iso_date}"]'
    try:
        _vprint("[search] waiting for first date option", iso_date)
        await page.wait_for_selector(opt_sel, state="attached", timeout=10_000)
        _vprint("[search] selecting first date", iso_date)
        await page.select_option('select#form\\.date', iso_date)
        return True
    except Exception:
        _vprint("[search] first date missing from picker", iso_date)
        return False


async def _click_change_date(page: Page, iso_date: str) -> bool:
    target = f"changeDate('{iso_date}')"
    expected_label = datetime.strptime(iso_date, "%Y-%m-%d").strftime("%d %b")
    deadline = asyncio.get_running_loop().time() + 10
    _vprint("[search] looking for next-date button", target, "label:", expected_label)

    while asyncio.get_running_loop().time() < deadline:
        buttons = page.locator("button").filter(has_text=expected_label)
        count = await buttons.count()
        fallback_button = None
        _vprint("[search] matching visible-text candidates", expected_label, "=", count)

        for idx in range(count):
            button = buttons.nth(idx)
            wire_click = await button.get_attribute("wire:click.prevent")
            is_visible = await button.is_visible()
            text = _WS_RE.sub(" ", await button.inner_text()).strip()
            _vprint(
                "[search] candidate",
                idx,
                "wire:",
                wire_click,
                "visible:",
                is_visible,
                "text:",
                text,
            )
            if wire_click != target:
                continue
            if not is_visible:
                continue

            if expected_label in text:
                _vprint("[search] clicking next-date button", iso_date)
                await button.click()
                return True

            if fallback_button is None:
                fallback_button = button

        if fallback_button is not None:
            _vprint("[search] clicking fallback next-date button", iso_date)
            await fallback_button.click()
            return True

        await page.wait_for_timeout(500)

    _vprint("[search] no next-date button found for", iso_date)
    return False

async def _lines_for_date(page: Page, iso_date: str, first_date: bool = False) -> Set[str]:
    if first_date:
        # Prepare the search and select the first date
        await _prepare_search(page)
        ok = await _select_date_safe(page, iso_date)
        if not ok:
            if VERBOSE:
                print("⏭  skipping", iso_date, "(not in picker)")
            return set()
        await page.click('button:has-text("Search")')

    else:
        clicked = await _click_change_date(page, iso_date)
        if not clicked:
            raise RuntimeError(f"Could not find next-date button for {iso_date}")
        



    # Adaptive wait – reduced attempts, shorter default wait
    lines: Set[str] = set()
    for attempt in range(SEARCH_ATTEMPTS):
        await page.wait_for_timeout(SEARCH_WAIT_MS)
        html = await page.content()
        new = set(html_to_lines(html))
        if new or attempt == SEARCH_ATTEMPTS - 1:
            lines = new
            break
    if VERBOSE:
        print(f"✓ {iso_date}: {len(lines)} line(s)")
    return lines


async def _lines_for_date(page: Page, iso_date: str, first_date: bool = False) -> Set[str]:
    if first_date:
        _vprint("[search] preparing first-day search", iso_date)
        await _prepare_search(page)
        ok = await _select_date_safe(page, iso_date)
        if not ok:
            _vprint("[search] skipping first day", iso_date, "(not in picker)")
            return set()
        _vprint("[search] clicking Search for first day", iso_date)
        await page.click('button:has-text("Search")')
        _vprint("[search] Search clicked for first day", iso_date, "url:", page.url)
        await page.wait_for_timeout(SEARCH_WAIT_MS)
        await _dump_first_day_page(page, iso_date)
    else:
        clicked = await _click_change_date(page, iso_date)
        if not clicked:
            raise RuntimeError(f"Could not find next-date button for {iso_date}")

    lines: Set[str] = set()
    for attempt in range(SEARCH_ATTEMPTS):
        if not first_date or attempt > 0:
            await page.wait_for_timeout(SEARCH_WAIT_MS)
        html = await page.content()
        new = set(html_to_lines(html))
        if new or attempt == SEARCH_ATTEMPTS - 1:
            lines = new
            break
    _vprint(f"✓ {iso_date}: {len(lines)} line(s)")
    return lines


async def initialize_browser() -> Tuple[Browser, Page]:
    """Initialize the browser and return the browser and page objects."""
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    page = await browser.new_page()
    return browser, page



async def login(page: Page) -> None:
    """Perform the login process."""
    await _login(page)



async def fetch_all_slots(page: Page) -> List[str]:
    today = date.today()
    start_date = today + timedelta(days=DATE_START_OFFSET_DAYS)
    iso_dates = [
        (start_date + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(DATE_WINDOW_DAYS + 1)
    ]

    seen: Set[str] = set()

    for i, iso in enumerate(iso_dates):
        first_date = (i == 0)  # True for the first date, False for subsequent dates
        seen.update(await _lines_for_date(page, iso, first_date=first_date))

    return sorted(seen)


async def close_browser(browser: Browser) -> None:
    """Close the browser."""
    await browser.close()

# ───────────────────────────
#  Snapshot + e‑mail helpers
# ───────────────────────────

def load_previous() -> Set[str]:
    try:
        raw = SNAP_PATH.read_text(encoding="utf-8")
        return {_canonical(l) for l in raw.splitlines()}
    except FileNotFoundError:
        return set()


def save_current(lines: List[str]) -> None:
    SNAP_PATH.write_text("\n".join(lines), encoding="utf-8")


def send_email(new_lines: List[str]) -> None:
    if not new_lines or not all(SMTP.values()):
        return
    msg = EmailMessage()
    msg["Subject"] = f"[Ravenair Cloud] {len(new_lines)} new booking slot(s)"
    msg["From"]    = SMTP["SMTP_USER"]
    msg["To"]      = SMTP["SMTP_TO"]
    msg.set_content("\n".join(new_lines))

    with smtplib.SMTP(SMTP["SMTP_HOST"], int(SMTP["SMTP_PORT"]), timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(SMTP["SMTP_USER"], SMTP["SMTP_PASS"])
        s.send_message(msg)
    print(f"📧  Sent {len(new_lines)} new slot(s) to {SMTP['SMTP_TO']}")

# ───────────────────────────
#  Main
# ───────────────────────────

async def main() -> None:
    browser, page = await initialize_browser()
    try:
        await login(page)
        current = await fetch_all_slots(page)
        prev = load_previous()
        new = [ln for ln in current if ln not in prev]

        # Send email for new slots
        send_email(new)

        # Save the current slots for future runs
        save_current(current)

        # Print summary
        print(f"✓ {len(current)} total slots   •   {len(new)} new since last run")
        for ln in new:
            print("   +", ln)
    finally:
        await close_browser(browser)


if __name__ == "__main__":
    asyncio.run(main())
