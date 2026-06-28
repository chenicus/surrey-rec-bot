"""
Surrey Rec Registration Bot — Hybrid approach
  • Login:     Playwright headless (LoginRadius works fine headless)
  • Attendee:  requests.get() → parse server-rendered HTML (bypasses JS headless detection)
  • FillForms: requests.post() → get shoppingCartKey from redirect
  • Checkout:  Playwright → access cross-origin iframe via CDP frames → click Place My Order

Why hybrid?
  PerfectMind's attendee page runs client-side JS that detects headless Chrome and
  *removes* the form inputs from the DOM.  But the server HTML always contains them.
  requests.get() sees the raw server HTML before any JS runs — 100% reliable.
"""

import asyncio
import json
import os
import re
import urllib.parse
from datetime import datetime

import requests as rq
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

load_dotenv()

EMAIL      = os.getenv("SURREY_EMAIL")
PASSWORD   = os.getenv("SURREY_PASSWORD")
CONTACT_ID = os.getenv("SURREY_CONTACT_ID", "283f188a-f820-428b-a38d-9d5b325291f8")

BASE        = "https://cityofsurrey.perfectmind.com"
BOOKING_URL = (
    f"{BASE}/23615/Clients/BookMe4BookingPages/Classes"
    "?calendarId=be083bfc-aeee-4c7a-aa26-07eb679e18a6"
    "&widgetId=b4059e75-9755-401f-a7b5-d7c75361420d"
    "&embed=False"
)
SESSION_FILE = "/tmp/surrey_session.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


def log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Login via Playwright (headless detection does NOT affect the login page) ──

async def _playwright_login():
    """Login via Playwright headless and save session cookies to SESSION_FILE."""
    log("Logging in via Playwright...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        )
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900}, user_agent=UA)
        page = await ctx.new_page()

        await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)

        login_btn = await page.query_selector(
            'a:has-text("Login"), button:has-text("Login"), '
            'a:has-text("Log In"), button:has-text("Log In")'
        )
        if not login_btn:
            log("Already logged in ✓")
            await ctx.storage_state(path=SESSION_FILE)
            await browser.close()
            return

        await login_btn.click()
        await asyncio.sleep(4)
        await page.wait_for_selector("#loginradius-login-emailid", timeout=15_000)
        await page.evaluate(
            """([e, p]) => {
                function sv(sel, v) {
                    const el = document.querySelector(sel);
                    const s = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                    s.call(el, v);
                    el.dispatchEvent(new Event('input',  {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                }
                sv('#loginradius-login-emailid', e);
                sv('#loginradius-login-password', p);
            }""",
            [EMAIL, PASSWORD],
        )
        await page.evaluate("document.querySelector('#loginradius-submit-login').click()")
        try:
            await page.wait_for_url("*perfectmind.com*", timeout=30_000)
        except PWTimeout:
            pass
        await asyncio.sleep(2)

        if "accounts.surrey.ca" in page.url or "auth.aspx" in page.url.lower():
            raise RuntimeError("Login failed — check SURREY_EMAIL / SURREY_PASSWORD")

        await ctx.storage_state(path=SESSION_FILE)
        log(f"Session saved ✓  ({page.url[:60]})")
        await browser.close()


# ── requests.Session loaded from Playwright cookies ───────────────────────────

def _make_requests_session() -> rq.Session:
    """Load Playwright session.json cookies into a requests.Session."""
    with open(SESSION_FILE) as f:
        state = json.load(f)
    session = rq.Session()
    session.headers.update({"User-Agent": UA})
    for c in state.get("cookies", []):
        session.cookies.set(
            c["name"], c["value"],
            domain=c.get("domain", ""), path=c.get("path", "/"),
        )
    return session


# ── Step 1: GET attendee page (server-rendered HTML) ─────────────────────────

def _get_attendee_html(session: rq.Session, event_id: str,
                       occurrence_date: str, widget_id: str,
                       location_id: str) -> tuple[str, str]:
    url = (
        f"{BASE}/23615/Clients/BookMe4EventParticipants"
        f"?eventId={event_id}&occurrenceDate={occurrence_date}"
        f"&widgetId={widget_id}&locationId={location_id}&waitListMode=False"
    )
    resp = session.get(url, allow_redirects=True, timeout=20)
    log(f"Attendee page → {resp.status_code}  {resp.url[:80]}")
    if resp.status_code != 200:
        raise RuntimeError(f"Attendee page returned {resp.status_code}")
    return resp.text, resp.url


# ── Step 2: Parse form + POST to FillForms ────────────────────────────────────

def _build_and_post_fillforms(session: rq.Session, html: str,
                               attendee_url: str) -> rq.Response:
    """
    Parse the server HTML to extract all FillForms fields, set David as
    participating, and POST.  Uses a list of tuples (not a dict) to preserve
    duplicate field names (ASP.NET MVC checkbox pattern: send 'true' then 'false').
    """
    soup = BeautifulSoup(html, "html.parser")
    forms = soup.find_all("form")
    if len(forms) < 2:
        raise RuntimeError(f"Expected ≥2 forms in HTML, got {len(forms)}")

    fill_form = forms[1]   # index 0 = AjaxAntiForgeryForm (1 field), index 1 = FillForms (41 fields)
    action = fill_form.get("action", "")
    if not action:
        raise RuntimeError("FillForms form has no action attribute")
    if action.startswith("/"):
        action = BASE + action
    log(f"FillForms action: {action}")

    david_sent = False
    data: list[tuple[str, str]] = []

    for inp in fill_form.find_all("input"):
        name  = inp.get("name", "")
        typ   = inp.get("type", "text").lower()
        value = inp.get("value", "")

        if not name or typ == "submit":
            continue

        if typ == "checkbox":
            if "FamilyMembers[1]" in name and "IsParticipating" in name:
                # David Chen → checked: send 'true' first (ASP.NET reads first value)
                data.append((name, "true"))
                david_sent = True
            elif inp.get("checked"):
                # Other checkboxes that are pre-checked in HTML
                data.append((name, value or "true"))
            # Unchecked checkboxes: omit — the paired hidden 'false' input handles it
        else:
            data.append((name, value))

    if not david_sent:
        log("WARNING: David's IsParticipating checkbox not found in parsed HTML")

    log(f"Posting {len(data)} fields to FillForms...")
    resp = session.post(
        action, data=data,
        allow_redirects=True, timeout=30,
        headers={"Referer": attendee_url},
    )
    log(f"FillForms → {resp.status_code}  {resp.url[:100]}")
    return resp


# ── Step 3: Playwright checkout (click Place My Order in cross-origin iframe) ─

async def _playwright_checkout(shopping_cart_key: str) -> bool:
    user_name_enc = urllib.parse.quote(EMAIL or "", safe="")
    checkout_url = (
        f"{BASE}/23615/Menu/SocialSite/MemberCheckout"
        f"?shoppingCartKey={shopping_cart_key}&userName={user_name_enc}"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--single-process"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            storage_state=SESSION_FILE,
            user_agent=UA,
        )
        page = await ctx.new_page()

        log(f"Checkout → {checkout_url[:80]}")
        await page.goto(checkout_url, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(5)   # wait for cross-origin iframe to load

        frame_urls = [f.url[:60] for f in page.frames]
        log(f"Frames ({len(frame_urls)}): {frame_urls}")

        # Find the online-store / store-ca frame
        checkout_frame = None
        for frame in page.frames:
            if any(k in frame.url for k in ("store", "checkout", "cart", "payment", "order")):
                checkout_frame = frame
                break
        if not checkout_frame and len(page.frames) > 1:
            checkout_frame = page.frames[1]
            log(f"Using fallback frame: {checkout_frame.url[:60]}")

        if not checkout_frame:
            log("No checkout frame found")
            await browser.close()
            return False

        log(f"Checkout frame: {checkout_frame.url[:70]}")

        try:
            await checkout_frame.wait_for_selector(
                "button, input[type=submit], a", timeout=10_000
            )
        except PWTimeout:
            frame_text = await checkout_frame.evaluate("() => document.body?.innerText || ''")
            log(f"No interactive elements in checkout frame. Text: {frame_text[:300]}")
            await browser.close()
            return False

        placed = await checkout_frame.evaluate("""
            () => {
                const btn = [...document.querySelectorAll('button, input[type=submit], a')]
                    .find(el =>
                        /place.*order|complete.*order|confirm|checkout/i.test(
                            (el.textContent || el.value || '').trim()
                        )
                    );
                if (btn) {
                    btn.click();
                    return (btn.textContent || btn.value || '').trim();
                }
                return 'NO_MATCH:' + [...document.querySelectorAll(
                    'button, input[type=submit], a'
                )].slice(0, 15).map(el =>
                    (el.textContent || el.value || '').trim().substring(0, 40)
                ).filter(Boolean).join(' | ');
            }
        """)
        log(f"Checkout click: {placed!r}")

        if placed and not placed.startswith("NO_MATCH"):
            await asyncio.sleep(4)
            log(f"Final URL: {page.url[:80]}")
            await browser.close()
            return True

        # Fallback: check if main page shows success already
        main_text = await page.evaluate("() => document.body.innerText")
        if any(w in main_text.lower() for w in ("success", "thank", "confirmed", "booked", "receipt")):
            log("Registration confirmed ✓  (success text on main page)")
            await browser.close()
            return True

        log(f"Could not click Place My Order. Buttons found: {placed}")
        await browser.close()
        return False


# ── Public entry point ────────────────────────────────────────────────────────

async def register(event_id: str, occurrence_date: str,
                   widget_id: str, location_id: str) -> bool:
    """
    Register David Chen for a drop-in session.
    Returns True on success, False on failure.
    """
    if not EMAIL or not PASSWORD:
        raise RuntimeError("SURREY_EMAIL and SURREY_PASSWORD must be set")

    # Ensure we have a valid session
    if not os.path.exists(SESSION_FILE):
        await _playwright_login()

    session = _make_requests_session()

    # GET attendee page (raw server HTML — no JS headless detection here)
    try:
        html, attendee_url = _get_attendee_html(
            session, event_id, occurrence_date, widget_id, location_id
        )
    except Exception as e:
        log(f"Attendee page failed: {e} — re-logging in...")
        if os.path.exists(SESSION_FILE):
            os.remove(SESSION_FILE)
        await _playwright_login()
        session = _make_requests_session()
        html, attendee_url = _get_attendee_html(
            session, event_id, occurrence_date, widget_id, location_id
        )

    # Already registered?
    if re.search(r"already\s+(registered|booked)", html, re.I):
        log("Already registered ✓")
        return True

    # POST FillForms
    try:
        fill_resp = _build_and_post_fillforms(session, html, attendee_url)
    except Exception as e:
        log(f"FillForms failed: {e}")
        return False

    if re.search(r"already\s+(registered|booked)", fill_resp.text, re.I):
        log("Already registered ✓  (post-FillForms)")
        return True

    # Extract shoppingCartKey from the redirect chain
    cart_key = re.search(r"shoppingCartKey=([^&\s]+)", fill_resp.url)
    cart_key = cart_key.group(1) if cart_key else None

    if not cart_key:
        # No cart key — check if the response itself is a success page
        if any(w in fill_resp.text.lower() for w in ("success", "thank", "confirmed", "receipt")):
            log("Registration confirmed ✓  (no cart key, success text)")
            return True
        log(f"No shoppingCartKey in redirect: {fill_resp.url[:100]}")
        return False

    log(f"Cart key: {cart_key}")

    # Complete checkout via Playwright iframe
    return await _playwright_checkout(cart_key)
