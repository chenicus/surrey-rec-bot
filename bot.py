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
    await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30_000)
    # Give JS widgets a moment to render (LoginRadius injects dynamically)
    await asyncio.sleep(3)
    log(f"Landed on: {page.url}")

    # Check if already logged in (no Login button visible)
    login_btn = await page.query_selector('a:has-text("Login"), button:has-text("Login"), a:has-text("Log In"), button:has-text("Log In"), a:has-text("Sign In")')
    if not login_btn:
        log("Already logged in ✓")
        return

    # Click the Login button to open the login form/modal
    log("Clicking Login button...")
    await login_btn.click()
    # Wait for LoginRadius form to inject (it's JS-rendered)
    await asyncio.sleep(4)
    log(f"After login click: {page.url}")

    # Wait for LoginRadius form to render (JS-injected, confirmed ID: #loginradius-login-emailid)
    try:
        await page.wait_for_selector("#loginradius-login-emailid", timeout=15_000)
        log("LoginRadius form ready")
    except PWTimeout:
        content = await page.content()
        log(f"Current URL: {page.url}")
        log(f"Page snippet: {content[1000:2500]}")
        raise RuntimeError(f"LoginRadius form did not appear (url={page.url})")

    # Use the native value setter — this is what LoginRadius actually requires.
    # Playwright's fill()/type() don't always trigger LoginRadius's React event handlers,
    # but setting value via the native HTMLInputElement prototype setter + dispatching
    # input/change events works reliably (verified manually).
    log("Filling credentials via native value setter...")
    await page.evaluate(
        """([email, password]) => {
            function setVal(sel, value) {
                const el = document.querySelector(sel);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    window.HTMLInputElement.prototype, 'value').set;
                setter.call(el, value);
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }
            setVal('#loginradius-login-emailid', email);
            setVal('#loginradius-login-password', password);
        }""",
        [EMAIL, PASSWORD],
    )
    log(f"Credentials set for {EMAIL}")
    await asyncio.sleep(1)

    # Click the Sign In button
    log("Clicking Sign In...")
    await page.click('#loginradius-submit-login')

    # After submitting credentials, wait for redirect back to perfectmind.com
    # (accounts.surrey.ca never reaches networkidle — it has ongoing analytics calls)
    try:
        await page.wait_for_url("*perfectmind.com*", timeout=30_000)
    except PWTimeout:
        pass
    await asyncio.sleep(2)
    log(f"After login URL: {page.url}")

    if "accounts.surrey.ca" in page.url or "auth.aspx" in page.url.lower():
        raise RuntimeError("Login failed — still on auth page. Check SURREY_EMAIL / SURREY_PASSWORD")
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


SESSION_FILE = "/tmp/surrey_session.json"


async def register(class_name: str, location: str) -> bool:
    """
    Main entry point. Returns True if registration succeeded.
    Reuses a saved browser session so login only happens once per process lifetime.
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

        # Load saved session if it exists (avoids re-login on every run)
        import os as _os
        storage = SESSION_FILE if _os.path.exists(SESSION_FILE) else None
        if storage:
            log(f"Loading saved session from {SESSION_FILE}")
        ctx  = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=storage,
        )
        page = await ctx.new_page()

        success = False
        try:
            await _login(page)

            # Save session after successful login so next run skips login
            await ctx.storage_state(path=SESSION_FILE)
            log("Session saved ✓")

            # Load the booking page
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30_000)
            await asyncio.sleep(3)

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
            # If login failed, delete stale session so next run tries fresh
            if "Login failed" in str(e) or "LoginRadius form" in str(e):
                if _os.path.exists(SESSION_FILE):
                    _os.remove(SESSION_FILE)
                    log("Deleted stale session file")
            raise
        finally:
            await browser.close()

    return success
