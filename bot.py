"""
Surrey Rec Registration Bot — Cloud Version
Runs headless (no visible browser window), designed for Railway/Linux.
"""

import asyncio
import os
from datetime import datetime
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()

EMAIL    = os.getenv("SURREY_EMAIL")
PASSWORD = os.getenv("SURREY_PASSWORD")

LOGIN_URL   = "https://cityofsurrey.perfectmind.com/23615/Account/LogIn"
BOOKING_URL = (
    "https://cityofsurrey.perfectmind.com/23615/Clients/BookMe4BookingPages/Classes"
    "?calendarId=be083bfc-aeee-4c7a-aa26-07eb679e18a6"
    "&widgetId=b4059e75-9755-401f-a7b5-d7c75361420d"
    "&embed=False"
)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}", flush=True)


async def _login(page):
    log("Logging in...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20_000)
    await page.fill('input[id*="Email"], input[name*="Email"], input[type="email"]', EMAIL)
    await page.fill('input[id*="Password"], input[name*="Password"], input[type="password"]', PASSWORD)
    await page.click('button[type="submit"], input[type="submit"], button:has-text("Sign In"), button:has-text("Log In")')
    await page.wait_for_load_state("networkidle", timeout=20_000)
    if "login" in page.url.lower() or "signin" in page.url.lower():
        raise RuntimeError("Login failed — check SURREY_EMAIL / SURREY_PASSWORD")
    log("Logged in ✓")


async def _find_and_click(page, class_name: str, location: str) -> bool:
    try:
        await page.wait_for_selector(
            ".booking-item, .class-row, [class*='ClassCard'], [class*='class-item'], [class*='card']",
            timeout=8_000,
        )
    except PWTimeout:
        log("No rows visible yet.")

    rows = await page.query_selector_all(
        ".booking-item, .class-row, [class*='ClassCard'], [class*='class-item'], "
        "[class*='card'], li[class*='event'], tr[class*='row'], div[class*='card']"
    )
    log(f"Scanning {len(rows)} rows for '{class_name}' @ '{location}'...")

    for row in rows:
        try:
            text = (await row.inner_text()).strip()
        except Exception:
            continue
        if class_name.lower() not in text.lower():
            continue
        if location.lower() not in text.lower():
            continue

        log(f"Matched: {text[:100]!r}")
        btn = await row.query_selector(
            "button, a[href*='book'], a[href*='register'], "
            "a[class*='register'], a[class*='book'], button[class*='register']"
        )
        if not btn:
            try:
                parent = await row.evaluate_handle(
                    "el => el.closest('tr, li, .card, .item') || el.parentElement"
                )
                btn = await parent.query_selector("button, a[href*='book'], a[href*='register']")
            except Exception:
                pass

        if btn:
            log(f"Clicking: {(await btn.inner_text()).strip()!r}")
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=10_000)
            return True
        else:
            log("Row matched but no button (full or already registered).")

    return False


async def _confirm(page) -> bool:
    content = (await page.content()).lower()
    if any(w in content for w in ("success", "you are registered", "booking confirmed", "thank you")):
        log("Confirmed ✓")
        return True
    btn = await page.query_selector(
        "button:has-text('Confirm'), button:has-text('Submit'), "
        "button:has-text('Complete'), input[value='Confirm'], input[value='Submit']"
    )
    if btn:
        await btn.click()
        await page.wait_for_load_state("networkidle", timeout=10_000)
        return True
    return False


async def register(class_name: str, location: str) -> bool:
    """
    Main entry point. Returns True if registration succeeded.
    """
    if not EMAIL or not PASSWORD:
        raise RuntimeError("SURREY_EMAIL and SURREY_PASSWORD environment variables are not set.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--single-process",
            ],
        )
        ctx  = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        success = False
        try:
            await _login(page)
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=15_000)
            await page.reload(wait_until="domcontentloaded", timeout=10_000)

            for attempt in range(1, 6):
                log(f"Attempt {attempt}/5")
                found = await _find_and_click(page, class_name, location)
                if found:
                    success = await _confirm(page)
                    if success:
                        log(f"✅ Registered: {class_name} @ {location}")
                        break
                if attempt < 5:
                    await asyncio.sleep(4)
                    await page.reload(wait_until="domcontentloaded", timeout=10_000)

            if not success:
                log(f"❌ Could not register: {class_name} @ {location}")

        except Exception as e:
            log(f"ERROR: {e}")
            raise
        finally:
            await browser.close()

    return success
