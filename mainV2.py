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
load_dotenv("variables.env")

EMAIL    = os.getenv("LOGIN_EMAIL")
PASSWORD = os.getenv("LOGIN_PASSWORD")
BASE     = "https://ravenair.volanti.club"

TYPE_VAL   = "5"           # Training (Dual)
COURSE_VAL = "3"           # UK PPL – PA28

DATE_WINDOW_DAYS = int(os.getenv("DATE_WINDOW_DAYS", "57"))      # inclusive
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

# ───────────────────────────
#  Playwright helpers
# ───────────────────────────
async def _login(page: Page) -> None:
    await page.goto(f"{BASE}/auth/login")
    if page.url.startswith(f"{BASE}/me"):
        return
    await page.fill('input[name="email"]',    EMAIL)
    await page.fill('input[name="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    await page.wait_for_url(f"{BASE}/me")


async def _prepare_search(page: Page) -> None:
    await page.goto(f"{BASE}/bookings/search")
    await page.select_option('select#data\\.type',   TYPE_VAL)
    await page.select_option('select#data\\.course', COURSE_VAL)


async def _select_date_safe(page: Page, iso_date: str) -> bool:
    opt_sel = f'select#data\\.date option[value="{iso_date}"]'
    try:
        await page.wait_for_selector(opt_sel, state="attached", timeout=10_000)
        await page.select_option('select#data\\.date', iso_date)
        return True
    except Exception:
        return False


async def _lines_for_date(page: Page, iso_date: str) -> Set[str]:
    await _prepare_search(page)
    ok = await _select_date_safe(page, iso_date)
    if not ok:
        if VERBOSE:
            print("⏭  skipping", iso_date, "(not in picker)")
        return set()

    await page.click('button:has-text("Search")')

    # Adaptive wait – reduced attempts, shorter default wait
    lines: Set[str] = set()
    for attempt in range(SEARCH_ATTEMPTS):
        await page.wait_for_timeout(SEARCH_WAIT_MS)
        html  = await page.content()
        new   = set(html_to_lines(html))
        if new or attempt == SEARCH_ATTEMPTS - 1:
            lines = new
            break
    if VERBOSE:
        print(f"✓ {iso_date}: {len(lines)} line(s)")
    return lines


async def fetch_all_slots() -> List[str]:
    today = date.today()
    iso_dates = [
        (today + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(DATE_WINDOW_DAYS + 1)
    ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page    = await browser.new_page()
        await _login(page)

        seen: Set[str] = set()
        for iso in iso_dates:
            seen.update(await _lines_for_date(page, iso)


        await browser.close()

    return sorted(seen)

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
    current = await fetch_all_slots()
    prev    = load_previous()
    new     = [ln for ln in current if ln not in prev]

    send_email(new)
    save_current(current)

    print(f"✓ {len(current)} total slots   •   {len(new)} new since last run")
    for ln in new:
        print("   +", ln)


if __name__ == "__main__":
    asyncio.run(main())





