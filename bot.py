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
    # Navigate to the booking page first
    await page.goto(BOOKING_URL, wait_until="networkidle", timeout=30_000)
    log(f"Landed on: {page.url}")

    # Check if already logged in (no Login button visible)
    login_btn = await page.query_selector('a:has-text("Login"), button:has-text("Login"), a:has-text("Log In"), button:has-text("Log In"), a:has-text("Sign In")')
    if not login_btn:
        log("Already logged in ✓")
        return

    # Click the Login button to open the login form/modal
    log("Clicking Login button...")
    await login_btn.click()
    await page.wait_for_load_state("networkidle", timeout=15_000)
    log(f"After login click: {page.url}")

    # Surrey uses LoginRadius widget — wait for it to render (JS-injected)
    EMAIL_SELECTORS = [
        # LoginRadius-specific
        "#loginradius-login-emailid",
        ".loginradius-user-emailid",
        'input[id*="loginradius"][id*="email" i]',
        'input[class*="loginradius"][id*="email" i]',
        # Generic fallbacks
        'input[id*="Email" i]',
        'input[name*="Email" i]',
        'input[type="email"]',
    ]
    PASSWORD_SELECTORS = [
        "#loginradius-login-password",
        ".loginradius-user-password",
        'input[id*="loginradius"][id*="password" i]',
        'input[id*="Password" i]',
        'input[name*="Password" i]',
        'input[type="password"]',
    ]

    # Wait up to 15s for any email field to appear (LoginRadius loads async)
    email_sel = None
    for sel in EMAIL_SELECTORS:
        try:
            await page.wait_for_selector(sel, timeout=15_000)
            email_sel = sel
            log(f"Found email field: {sel}")
            break
        except PWTimeout:
            continue

    if not email_sel:
        # Dump page content and URL to help debug
        content = await page.content()
        log(f"Current URL: {page.url}")
        log(f"Page snippet (2000-3500): {content[2000:3500]}")
        raise RuntimeError(f"Could not find email input on login page (url={page.url})")

    pass_sel = None
    for sel in PASSWORD_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el:
                pass_sel = sel
                break
        except Exception:
            continue

    await page.fill(email_sel, EMAIL)
    if pass_sel:
        await page.fill(pass_sel, PASSWORD)

    # Click submit
    submit_selectors = [
        'button[class*="loginradius-submit"]',
        'input[class*="loginradius-submit"]',
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Sign In")',
        'button:has-text("Log In")',
        'button:has-text("Login")',
    ]
    for sel in submit_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                break
        except Exception:
            continue

    await page.wait_for_load_state("networkidle", timeout=30_000)
    log(f"After login URL: {page.url}")

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
