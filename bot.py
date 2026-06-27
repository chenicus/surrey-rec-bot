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

    # Click the Sign In button via JS (element may not be "visible" to Playwright
    # even though it works fine — bypass actionability checks with evaluate)
    log("Clicking Sign In...")
    await page.evaluate("document.querySelector('#loginradius-submit-login').click()")

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


CONTACT_ID = os.getenv("SURREY_CONTACT_ID", "283f188a-f820-428b-a38d-9d5b325291f8")


async def _do_registration(page, class_name: str, location: str) -> bool:
    """
    Full 3-step registration flow (verified by watching the real site):
      1. Booking page  → find class card → click input.bm-class-details
      2. Detail page   → click a.bm-book-button
      3. Attendee page → check David Chen's checkbox → click Next
    """

    # ── Step 1: find the class card and click Register ──────────────────────
    try:
        await page.wait_for_selector(".bm-class-container", timeout=8_000)
    except PWTimeout:
        log("No class cards visible yet.")

    rows = await page.query_selector_all(".bm-class-container")
    log(f"Scanning {len(rows)} cards for '{class_name}' @ '{location}'...")

    clicked_card = False
    for row in rows:
        try:
            text = (await row.inner_text()).strip()
        except Exception:
            continue
        if class_name.lower() not in text.lower():
            continue
        if location.lower() not in text.lower():
            continue

        log(f"Matched card: {text[:100]!r}")
        btn = await row.query_selector("input.bm-class-details")
        if not btn:
            log("Card matched but no Register button (full?).")
            continue
        log("Step 1: clicking card Register button...")
        await page.evaluate("el => el.click()", btn)
        clicked_card = True
        break

    if not clicked_card:
        return False

    # ── Step 2: detail page — click the Register link ───────────────────────
    try:
        await page.wait_for_selector("a.bm-book-button", timeout=10_000)
    except PWTimeout:
        log("Detail page Register button not found.")
        return False

    log(f"Step 2: on detail page ({page.url[:60]}...)")
    # Check "Already Registered" or "Already Booked" here (success for a re-run)
    detail_text = (await page.content()).lower()
    if "already registered" in detail_text or "already booked" in detail_text:
        log("Already registered ✓")
        return True

    await page.evaluate("document.querySelector('a.bm-book-button').click()")
    await asyncio.sleep(3)

    # ── Step 3: Select Attendee page — check David Chen, click Next ──────────
    try:
        await page.wait_for_selector("input.member-id", timeout=10_000)
    except PWTimeout:
        log("Select Attendee page did not appear.")
        return False

    log(f"Step 3: on attendee page ({page.url[:60]}...)")

    result = await page.evaluate(f"""
        () => {{
            // Find David Chen by ContactId (MemberId field)
            const memberInputs = [...document.querySelectorAll('input.member-id')];
            const memberInput = memberInputs.find(el => el.value === '{CONTACT_ID}');
            if (!memberInput) return 'contact_not_found';

            // Derive the IsParticipating checkbox name from the MemberId field name
            const checkboxName = memberInput.name.replace('.MemberId', '.IsParticipating');
            const checkbox = document.querySelector(`input[name="${{checkboxName}}"][type="checkbox"]`);
            if (!checkbox) return 'checkbox_not_found';

            // Check attendance status — if already booked, count as success
            const statusName = memberInput.name.replace('.MemberId', '.AttendanceStatus');
            const statusInput = document.querySelector(`input[name="${{statusName}}"]`);
            if (statusInput && statusInput.value === 'Booked') return 'already_booked';

            if (checkbox.disabled) return 'checkbox_disabled';

            checkbox.click();
            return 'checked';
        }}
    """)
    log(f"Attendee selection result: {result}")

    if result in ("already_booked",):
        log("David Chen already registered ✓")
        return True
    if result not in ("checked",):
        log(f"Could not select David Chen: {result}")
        return False

    # Click the Next button (becomes enabled after checking the checkbox)
    await asyncio.sleep(1)
    next_clicked = await page.evaluate("""
        () => {
            const btn = document.querySelector('a.bm-button:not(.disabled)');
            if (btn) { btn.click(); return true; }
            return false;
        }
    """)
    if not next_clicked:
        log("Next button not enabled after selecting attendee.")
        return False

    log("Clicked Next — waiting for confirmation...")
    await asyncio.sleep(4)

    # ── Step 4: confirmation / checkout ─────────────────────────────────────
    final_text = (await page.content()).lower()
    log(f"Final page URL: {page.url[:80]}")
    if any(w in final_text for w in ("success", "confirmed", "thank you", "booked", "registered", "receipt", "checkout")):
        log("Registration confirmed ✓")
        return True

    # If there's a final Confirm/Submit button (e.g. zero-cost checkout), click it
    confirm = await page.evaluate("""
        () => {
            const sel = [
                'input[value="Confirm"]', 'input[value="Submit"]',
                'button:not(.disabled)', 'a.bm-button:not(.disabled)'
            ];
            for (const s of sel) {
                const el = document.querySelector(s);
                if (el && /confirm|submit|complete|next|pay/i.test(el.textContent + el.value)) {
                    el.click(); return el.textContent || el.value;
                }
            }
            return null;
        }
    """)
    if confirm:
        log(f"Clicked final button: {confirm!r}")
        await asyncio.sleep(3)
        return True

    log("Could not confirm registration — unknown final page state.")
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

            for attempt in range(1, 4):
                log(f"Attempt {attempt}/3")
                success = await _do_registration(page, class_name, location)
                if success:
                    log(f"✅ Registered: {class_name} @ {location}")
                    break
                if attempt < 3:
                    log("Retrying — reloading booking page...")
                    await asyncio.sleep(3)
                    await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30_000)
                    await asyncio.sleep(3)

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
