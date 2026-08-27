"""
fetch_ola_statement.py
======================
Stage 1 of the Ola sync pipeline.

Responsibilities
----------------
- Read baseline OTP from the SMSOLA Google Sheet.
- Launch Chrome with a persistent profile (reuses an existing session when
  still valid; only does the full OTP flow when logged out).
- Navigate to Accounting Details, select last Monday–Sunday, download the
  statement.
- Save the file to ./ola_downloads/ and return its path.
- On any unrecoverable failure: write a Failed row to ola_import_log and
  exit with a non-zero status so the scheduler / alerting notices.

Configuration (all via environment variables or .env)
------------------------------------------------------
OLA_PHONE_NUMBER      Ola-registered mobile that receives OTPs
OLA_SHEET_ID          Google Sheet ID for the SMSOLA OTP relay
OLA_DOWNLOAD_DIR      Where to save downloaded files   (default: ./ola_downloads)
OLA_PROFILE_DIR       Path for the persistent Chrome profile (default: ./ola_chrome_profile)
DATABASE_URL          Postgres connection string (same as main.py uses)
DB_HOST / DB_PORT / DB_NAME / DB_USER / DB_PASS  (alternative to DATABASE_URL)
"""

import os
import re
import io
import sys
import time
import json
import traceback
from typing import Optional
from datetime import datetime, timedelta
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import requests
import psycopg2
from playwright.sync_api import sync_playwright

# Gmail IMAP fallback (for GCP where direct download is blocked by Ola)
try:
    from gmail_imap_fetch import fetch_ola_xlsx_from_gmail
    _GMAIL_AVAILABLE = True
except ImportError:
    _GMAIL_AVAILABLE = False


# ── Load .env (same logic as main.py) ───────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if not _env_path.exists():
    _env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    with open(_env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
PHONE_NUMBER  = os.environ.get("OLA_PHONE_NUMBER", "7483731338")
SHEET_ID      = os.environ.get("OLA_SHEET_ID",     "1KrJ022-HfOBNnRVky7DBebCGm6jGcfk3OV3UqcHagIA")
CSV_URL       = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"
LOGIN_URL     = "https://partners.olacabs.com/public/login"
ACCOUNTING_URL = "https://operator.olacabs.com/accounting-details"
DOWNLOAD_DIR  = os.environ.get("OLA_DOWNLOAD_DIR",  os.path.join(os.path.dirname(__file__), "ola_downloads"))
PROFILE_DIR   = os.environ.get("OLA_PROFILE_DIR",   os.path.join(os.path.dirname(__file__), "ola_chrome_profile"))
SCREENSHOT_DIR = os.path.join(DOWNLOAD_DIR, "screenshots")

os.makedirs(DOWNLOAD_DIR,   exist_ok=True)
os.makedirs(PROFILE_DIR,    exist_ok=True)
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


# ── DB helpers ────────────────────────────────────────────────────────────────
def _get_db_conn():
    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        return psycopg2.connect(dsn=db_url)
    return psycopg2.connect(
        host=os.environ.get("DB_HOST"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
    )


def create_import_log_row(import_type: str, week_start, week_end, target_table: str, file_name: str = None) -> Optional[int]:
    """No-op stub (ola_import_log table removed)."""
    return None


def update_import_log(log_id: Optional[int], status: str, error_message: str = None, file_name: str = None):
    """No-op stub (ola_import_log table removed)."""
    pass


# ── OTP helpers ───────────────────────────────────────────────────────────────
def get_current_otp_from_sheet():
    try:
        res = requests.get(CSV_URL, timeout=10)
        if res.status_code == 200:
            import pandas as pd
            df = pd.read_csv(io.StringIO(res.text))
            if not df.empty:
                msg      = str(df.iloc[0, 0])
                date_col = str(df.iloc[0, 2]) if df.shape[1] >= 3 else ""
                match    = re.search(r'\b(\d{4,6})\b', msg)
                otp      = match.group(1) if match else None
                return otp, date_col, msg
    except Exception as e:
        print(f"[OTP] Sheet fetch warning: {e}")
    return None, None, ""


def fetch_otp_after_request(initial_otp, initial_date, wait_seconds=3, timeout=300, page=None, logger=print):
    """
    Poll the Google Sheet for a fresh OTP.

    Strategy:
      - Wait up to `timeout` seconds (default 5 min) for a new OTP to appear.
      - OTP is considered FRESH only if it was received within the last 3 minutes
        (prevents accepting a stale OTP from a previous login session).
      - Polls every 3 seconds.
    """
    logger(f"[OTP] Polling Google Sheet for new OTP on {PHONE_NUMBER} (timeout: {timeout}s)...")
    time.sleep(wait_seconds)
    start = time.time()

    def _is_fresh_otp(date_str: str, max_age_minutes: int = 3) -> bool:
        """Return True if the OTP timestamp in the sheet is within max_age_minutes from now."""
        try:
            # Sheet timestamp format: "August 27 2026 at 01:03PM" / "August 27, 2026 at 1:03 PM" / ISO
            cleaned = date_str.strip().replace(",", "")
            for fmt in [
                "%B %d %Y at %I:%M%p",
                "%B %d %Y at %I:%M %p",
                "%Y-%m-%d %H:%M:%S",
                "%d/%m/%Y %H:%M:%S",
                "%d-%m-%Y %H:%M:%S",
            ]:
                try:
                    parsed = datetime.strptime(cleaned, fmt)
                    age_minutes = (datetime.now() - parsed).total_seconds() / 60
                    return 0 <= age_minutes <= max_age_minutes
                except ValueError:
                    continue
        except Exception:
            pass
        # If we can't parse the timestamp, fall back to checking if it's different from initial
        return date_str != initial_date

    while time.time() - start < timeout:
        otp, date_str, _ = get_current_otp_from_sheet()

        # Accept OTP only if it's genuinely fresh (within 3 minutes) AND different from initial
        if otp and otp != initial_otp and date_str and _is_fresh_otp(date_str):
            logger(f"[OTP] ✓ Got fresh OTP: {otp} (at {date_str})")
            return otp

        # Also accept if it's the same OTP value but clearly a brand new timestamp (within 3 min)
        if otp and otp == initial_otp and date_str and _is_fresh_otp(date_str, max_age_minutes=1):
            logger(f"[OTP] ✓ Got fresh OTP (same value, new timestamp): {otp} (at {date_str})")
            return otp

        elapsed = int(time.time() - start)
        logger(f"[OTP] [{elapsed}s] Waiting for OTP... (current sheet OTP: {otp})")
        time.sleep(3)

    raise RuntimeError(
        f"OTP not received within {timeout}s for phone {PHONE_NUMBER}. "
        "Caller will reload page and retry."
    )


# ── Chrome / Playwright helpers ───────────────────────────────────────────────
def cleanup_chrome_locks():
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        p = os.path.join(PROFILE_DIR, lock)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def get_current_week_dates():
    today = datetime.today()
    current_monday = today - timedelta(days=today.weekday())
    return current_monday, today


def get_last_week_dates():
    today       = datetime.today()
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday


def click_day_vuetify(page, day_num, logger=print):
    """Click a specific day in the Vuetify date-picker calendar."""
    target = str(day_num)
    logger(f"[DATE] Clicking day {target} in Vuetify picker...")
    try:
        page.wait_for_selector(".v-picker--date", state="visible", timeout=8000)
    except Exception:
        page.wait_for_selector(".v-picker--date", state="attached", timeout=5000)
    page.wait_for_timeout(500)

    result = page.evaluate(f"""() => {{
        const tables = Array.from(document.querySelectorAll('.v-date-picker-table'));
        const visible = tables.find(t => t.offsetWidth > 0 && t.offsetHeight > 0);
        if (!visible) return {{ok: false, msg: 'no visible table'}};
        for (const btn of visible.querySelectorAll('button.v-btn')) {{
            const c = btn.querySelector('.v-btn__content');
            if (!c) continue;
            if (c.textContent.trim() !== '{target}') continue;
            if (btn.classList.contains('v-btn--disabled')) continue;
            btn.click();
            return {{ok: true}};
        }}
        return {{ok: false, msg: 'day not found'}};
    }}""")
    logger(f"[DATE] Result: {result}")
    return result.get("ok", False)


def select_date_vuetify(page, target_date: datetime, is_from: bool = True, logger=print):
    """Open date picker, navigate to target month if needed, and select day."""
    target_day = str(target_date.day)
    target_month_name = target_date.strftime("%B")
    label = "FROM" if is_from else "TO"
    logger(f"[DATE] Selecting {label} date: {target_date.strftime('%Y-%m-%d')}...")

    # Open date picker input safely
    clicked_open = False
    try:
        inputs = page.locator(".pickers input, input[placeholder*='date'], input[role='button']")
        count = inputs.count()
        if is_from and count >= 1:
            inputs.nth(0).click(timeout=3000)
            clicked_open = True
        elif not is_from and count >= 2:
            inputs.nth(1).click(timeout=3000)
            clicked_open = True
        elif not is_from and count == 1:
            inputs.nth(0).click(timeout=3000)
            clicked_open = True
    except Exception:
        pass

    if not clicked_open:
        target_idx = 0 if is_from else 1
        page.evaluate(f"""(idx) => {{
            const inps = document.querySelectorAll('.pickers input, input[placeholder*="date"], input[role="button"]');
            if (inps[idx]) inps[idx].click();
            else if (inps[0]) inps[0].click();
        }}""", target_idx)

    page.wait_for_timeout(1000)

    # Check visible month & year and navigate to target month dynamically (up to 24 months)
    target_month_year = f"{target_date.strftime('%B %Y').lower()}"
    logger(f"[DATE] Target Month/Year: {target_month_year}")

    for nav_step in range(24):
        header_text = page.evaluate("""() => {
            const h = document.querySelector('.v-date-picker-header__value, .v-date-picker-header');
            return h ? h.textContent.trim().toLowerCase() : '';
        }""")
        
        # Check if month and year match target
        if target_date.strftime('%B').lower() in header_text and str(target_date.year) in header_text:
            logger(f"[DATE] Reached target month/year: {header_text}")
            break

        # Check if year is different or month is ahead -> navigate backwards
        # First header button is 'previous month', last header button is 'next month'
        clicked = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll('.v-date-picker-header button'));
            if (btns.length === 0) return false;
            btns[0].click(); // Previous month
            return true;
        }""")
        if not clicked:
            break
        page.wait_for_timeout(400)

    click_day_vuetify(page, target_day, logger)
    page.wait_for_timeout(1500)


def ss(page, name, logger=print):
    path = os.path.join(SCREENSHOT_DIR, f"{name}.png")
    try:
        page.screenshot(path=path)
        logger(f"[SS] {path}")
    except Exception:
        pass


# ── Main download function ────────────────────────────────────────────────────
def fetch_ola_statement(log_id: int = None, from_date: Optional[datetime] = None, to_date: Optional[datetime] = None, logger=print) -> str:
    """
    Log in to the Ola portal and download statement for the specified date window.

    Returns
    -------
    str  — absolute path to the saved file.
    """
    if from_date is None or to_date is None:
        yesterday = datetime.today() - timedelta(days=1)
        from_date = yesterday
        to_date = yesterday
    logger(f"[FETCH] Target Date Window: {from_date.strftime('%a %d %b %Y')} → {to_date.strftime('%a %d %b %Y')}")

    cleanup_chrome_locks()
    initial_otp, initial_date, _ = get_current_otp_from_sheet()
    logger(f"[FETCH] Baseline OTP in sheet: '{initial_otp}' (at {initial_date})")

    is_headless = os.environ.get("HEADLESS", "true").lower() == "true"
    with sync_playwright() as p:
        logger(f"[FETCH] Launching Chromium (headless={is_headless}) with persistent profile...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=is_headless,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
            permissions=["geolocation", "notifications"],
            geolocation={"latitude": 12.9716, "longitude": 77.5946},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            accept_downloads=True,
            downloads_path=DOWNLOAD_DIR,   # route browser downloads → ola_downloads/
            viewport={"width": 1366, "height": 768},
        )
        page = context.pages[0] if context.pages else context.new_page()

        # Inject stealth anti-detection overrides
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en-US', 'en']});
        """)

        # ── STEP 1-7: Login & Session Authentication Loop ──────────────────────
        logged_in = False
        for login_attempt in range(1, 4):
            logger(f"[FETCH] Login Attempt {login_attempt}/3...")
            try:
                page.goto(LOGIN_URL, timeout=30000)
                page.wait_for_timeout(5000)
            except Exception as load_err:
                logger(f"[FETCH] Page goto warning: {load_err}")

            if "login" not in page.url.lower():
                logger(f"[FETCH] Active session detected! URL: {page.url}")
                page.goto(ACCOUNTING_URL, timeout=60000)
                page.wait_for_timeout(6000)
                if "login" not in page.url.lower():
                    logged_in = True
                    break

            ss(page, f"01_login_page_attempt_{login_attempt}", logger)

            # Click mobile login
            for btn_text in ["Login with mobile number", "mobile number", "Sign in", "Log in", "Login"]:
                try:
                    btn = page.locator(f"button:has-text('{btn_text}'), a:has-text('{btn_text}'), div:has-text('{btn_text}')").first
                    if btn.is_visible(timeout=3000):
                        btn.click()
                        logger(f"[FETCH] Clicked '{btn_text}' button")
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            phone_input = None
            for sel in ["#identification", "input[type='tel']", "input[placeholder*='mobile']", "input[placeholder*='phone']", "input[type='number']"]:
                try:
                    if page.locator(sel).first.is_visible(timeout=3000):
                        phone_input = sel
                        break
                except Exception:
                    pass

            if not phone_input:
                logger(f"[FETCH] Phone input not visible on attempt {login_attempt} (URL: {page.url}). Reloading login page...")
                ss(page, f"no_phone_input_attempt_{login_attempt}", logger)
                page.goto("https://partners.olacabs.com/public/login", timeout=30000)
                page.wait_for_timeout(4000)
                # Re-check after reload
                for btn_text in ["Login with mobile number", "mobile number", "Sign in", "Log in", "Login"]:
                    try:
                        btn = page.locator(f"button:has-text('{btn_text}'), a:has-text('{btn_text}'), div:has-text('{btn_text}')").first
                        if btn.is_visible(timeout=3000):
                            btn.click()
                            page.wait_for_timeout(1500)
                            break
                    except Exception:
                        pass
                for sel in ["#identification", "input[type='tel']", "input[placeholder*='mobile']", "input[placeholder*='phone']", "input[type='number']"]:
                    try:
                        if page.locator(sel).first.is_visible(timeout=3000):
                            phone_input = sel
                            break
                    except Exception:
                        pass

            if not phone_input:
                logger(f"[FETCH] Still could not find phone input on attempt {login_attempt}.")
                continue

            logger(f"[FETCH] Entering phone: {PHONE_NUMBER}")
            page.fill(phone_input, PHONE_NUMBER)
            page.wait_for_timeout(500)

            initial_otp, initial_date, _ = get_current_otp_from_sheet()
            try:
                page.click("text=Continue", timeout=5000)
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(3000)

            logger("[FETCH] Waiting for OTP from sheet...")
            try:
                otp_code = fetch_otp_after_request(initial_otp, initial_date, wait_seconds=3, timeout=180, page=page, logger=logger)
            except Exception as otp_err:
                logger(f"[FETCH] OTP fetch failed on attempt {login_attempt}: {otp_err}")
                continue

            logger(f"[FETCH] Submitting OTP: {otp_code}")
            page.wait_for_selector("#otp", timeout=10000)
            page.fill("#otp", "")  # clear first
            page.type("#otp", str(otp_code), delay=120)
            page.wait_for_timeout(800)

            clicked_signin = page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const target = btns.find(b => {
                    const t = b.textContent.trim().toLowerCase();
                    return t.includes('sign in') || t.includes('login') || t.includes('submit') || t.includes('verify');
                });
                if (target) {
                    target.click();
                    return target.textContent.trim();
                }
                return null;
            }""")
            if not clicked_signin:
                page.keyboard.press("Enter")

            logger(f"[FETCH] Clicked Sign in button: '{clicked_signin}'")
            logger("[FETCH] Waiting for session redirect (up to 20s)...")

            # Wait for URL to leave login page (network-idle aware)
            try:
                page.wait_for_url(lambda url: "login" not in url.lower(), timeout=20000)
                logger(f"[FETCH] ✓ URL changed to: {page.url}")
            except Exception:
                logger(f"[FETCH] URL still at login after 20s: {page.url}")

            logger(f"[FETCH] Current URL post sign-in: {page.url}")

            # Try navigating to Accounting Details naturally (Dashboard "Know More" -> Sidebar -> Direct URL)
            reached = False
            for goto_attempt in range(1, 4):
                logger(f"[FETCH] Navigating to Accounting Details (try {goto_attempt}/3)...")

                # If on Dashboard, simulate human scroll and click "Know More" in Accounting Details card
                if "accounting" not in page.url.lower() and "login" not in page.url.lower():
                    logger(f"[FETCH] Currently on Dashboard ({page.url}). Simulating human scroll & finding 'Know More'...")
                    try:
                        page.wait_for_timeout(1500)
                        page.mouse.wheel(0, 300)
                        page.wait_for_timeout(1000)

                        # Look for "Know More" link in Accounting card
                        know_more = page.locator("a:has-text('Know More'), button:has-text('Know More'), .know-more").first
                        if know_more.is_visible(timeout=3000):
                            know_more.hover()
                            page.wait_for_timeout(500)
                            know_more.click()
                            logger("[FETCH] ✓ Clicked 'Know More' in Accounting Details card!")
                            page.wait_for_timeout(4000)
                    except Exception as km_err:
                        logger(f"[FETCH] 'Know More' click warning: {km_err}")

                # If still not on accounting page, try sidebar "Accounting" or "Banking"
                if "accounting" not in page.url.lower() and "login" not in page.url.lower():
                    try:
                        sidebar_btn = page.locator("a:has-text('Accounting'), div:has-text('Accounting'), span:has-text('Accounting')").first
                        if sidebar_btn.is_visible(timeout=2000):
                            sidebar_btn.click()
                            logger("[FETCH] Clicked 'Accounting' in sidebar menu")
                            page.wait_for_timeout(3000)
                    except Exception:
                        pass

                # Fallback: direct goto if still not reached
                if "accounting" not in page.url.lower():
                    try:
                        page.goto(ACCOUNTING_URL, timeout=60000, wait_until="networkidle")
                    except Exception as nav_err:
                        logger(f"[FETCH] goto networkidle error (non-fatal): {nav_err}")
                    page.wait_for_timeout(4000)

                ss(page, f"06_accounting_page_attempt_{login_attempt}_goto{goto_attempt}", logger)
                if "login" not in page.url.lower():
                    logger("[FETCH] ✓ Successfully reached Accounting Details page!")
                    reached = True
                    break
                logger(f"[FETCH] Still on login after goto {goto_attempt}. Waiting 5s...")
                page.wait_for_timeout(5000)

            if reached:
                logged_in = True
                break

        if not logged_in:
            context.close()
            raise RuntimeError("Failed to maintain logged-in session on OLA portal after max relogin attempts.")


        # ── STEP 8 + 9: Download with full retry logic ───────────────────────
        # Strategy (per client spec):
        #   Attempt 1: Custom Date (today) → try direct download / email modal
        #   Attempt 2: Custom Date (today) → retry
        #   Fallback 1: Today filter → try direct download / email modal
        #   Fallback 2: Custom Date (today) → retry
        #   Fallback 3: Today filter → retry
        #   → If all 5 attempts fail, raise and end.
        #
        # Before every retry: check IMAP first — if a report already arrived
        # within 60 min since pipeline started, use it immediately.
        #
        # Email: vendor_aayush@letzryd.com
        # ──────────────────────────────────────────────────────────────────
        EMAIL         = os.environ.get("GMAIL_IMAP_USER", os.environ.get("OLA_EMAIL", "vendor_aayush@letzryd.com"))
        today         = datetime.today()
        pipeline_start_ts = time.time()   # used to bound IMAP lookback window

        # ── Cleanup: delete temp UUID files and old xlsx files from ola_downloads/ ─
        def _cleanup_temp_downloads(lgr):
            """
            1. Delete Playwright UUID temp files left behind on failed attempts.
            2. Delete any .xlsx files from previous days/runs so that only the single
               active file (ola_statement_YYYY-MM-DD.xlsx) exists in ola_downloads/.
            """
            import re
            uuid_pattern = re.compile(
                r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                re.IGNORECASE
            )
            current_fname = f"ola_statement_{today.strftime('%Y-%m-%d')}.xlsx"
            removed_temp = 0
            removed_old = 0
            for fname in os.listdir(DOWNLOAD_DIR):
                fpath = os.path.join(DOWNLOAD_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                if uuid_pattern.match(fname) or fname.endswith(".crdownload") or fname.endswith(".tmp"):
                    try:
                        os.remove(fpath)
                        removed_temp += 1
                    except Exception:
                        pass
            if removed_temp:
                lgr(f"[FETCH] 🧹 Cleaned up {removed_temp} temp file(s) from ola_downloads/")

        # ── Helper: submit email modal ────────────────────────────────────
        def _handle_email_modal(pg, email_addr, lgr) -> bool:
            """Fill email in the export modal ONLY if an actual email export modal is visible."""
            try:
                # Check for explicit email input fields or dialog container
                modal_selectors = [
                    ".v-dialog",
                    "text=Get statement via email",
                    "text=Can we email it to you instead?",
                ]
                has_modal = False
                for sel in modal_selectors:
                    try:
                        if pg.locator(sel).first.is_visible(timeout=1000):
                            has_modal = True
                            break
                    except Exception:
                        pass

                email_input_loc = None
                input_selectors = [
                    "input[placeholder*='Enter Email']",
                    "input[placeholder*='email' i]",
                    "input[placeholder*='Email']",
                    ".v-dialog input[type='text']",
                    ".v-dialog input[type='email']",
                    ".v-dialog input",
                    "input[type='email']",
                ]
                for sel in input_selectors:
                    try:
                        loc = pg.locator(sel).first
                        if loc.is_visible(timeout=1000):
                            email_input_loc = loc
                            has_modal = True
                            break
                    except Exception:
                        pass

                if has_modal and email_input_loc:
                    lgr(f"[FETCH] 📧 Email export modal detected — entering email: {email_addr}")
                    try:
                        email_input_loc.click(timeout=3000)
                        email_input_loc.fill("")
                        email_input_loc.fill(email_addr)
                    except Exception as fill_err:
                        lgr(f"[FETCH] Playwright fill warning: {fill_err}")

                    # Fallback/supplement with JS evaluation to guarantee Vue v-model update
                    pg.evaluate("""(addr) => {
                        const inps = document.querySelectorAll('.v-dialog input, [role="dialog"] input, input[placeholder*="email" i], input[type="email"]');
                        for (const inp of inps) {
                            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                            if (setter) setter.call(inp, addr);
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            inp.dispatchEvent(new Event('blur', { bubbles: true }));
                        }
                    }""", email_addr)
                    pg.wait_for_timeout(800)

                    # Click SEND button
                    sent = False
                    for btn_sel in [
                        ".v-dialog button:has-text('SEND')",
                        "button:has-text('SEND')",
                        "button:has-text('Send')",
                        ".v-dialog button:has-text('SUBMIT')",
                        "button:has-text('SUBMIT')"
                    ]:
                        try:
                            btn = pg.locator(btn_sel).first
                            if btn.is_visible(timeout=1500):
                                btn.click()
                                sent = True
                                lgr(f"[FETCH] Clicked '{btn_sel}' button")
                                break
                        except Exception:
                            pass

                    if not sent:
                        sent = pg.evaluate("""() => {
                            const btns = Array.from(document.querySelectorAll('.v-dialog button, button'));
                            const btn = btns.find(b => ['SEND','SUBMIT','OKAY','OK','CONFIRM'].includes(b.textContent.trim().toUpperCase()));
                            if (btn) { btn.click(); return true; }
                            return false;
                        }""")

                    if sent:
                        pg.wait_for_timeout(2000)
                        lgr(f"[FETCH] ✓ Email export submitted to {email_addr}")
                        return True
            except Exception as _e:
                lgr(f"[FETCH] Email modal handler error: {_e}")
            return False

        # ── Helper: check for a fresh direct download ────────────────────
        def _check_direct_download(since_ts: float, lgr) -> str | None:
            """
            Scan DOWNLOAD_DIR (ola_downloads/ inside the repo) for a newly
            written .xlsx file created after since_ts.

            Production note (GCP / Docker):
              - Playwright routes browser downloads here via downloads_path=DOWNLOAD_DIR
              - IMAP fetch saves email attachments here via download_dir=DOWNLOAD_DIR
              - No system ~/Downloads dependency — safe for headless Linux containers.

            Returns absolute path to the file, or None if not found.
            """
            files = [
                os.path.join(DOWNLOAD_DIR, f)
                for f in os.listdir(DOWNLOAD_DIR)
                if f.endswith(".xlsx") and not f.startswith(".")
            ]
            if not files:
                return None
            latest = max(files, key=os.path.getmtime)
            if os.path.getmtime(latest) > since_ts:
                age = time.time() - os.path.getmtime(latest)
                lgr(f"[FETCH] ✓ Direct download in ola_downloads/: "
                    f"{os.path.basename(latest)} ({age:.0f}s ago)")
                return latest
            return None

        # ── Helper: poll IMAP for an already-arrived or incoming report ────
        def _poll_imap(lgr, lookback_minutes: int = 30,
                       max_wait_s: int = 300) -> str | None:
            if not _GMAIL_AVAILABLE:
                lgr("[FETCH] IMAP module not available — skipping Gmail poll")
                return None
            lgr(f"[FETCH] Polling Gmail ({EMAIL}) for Ola statement (up to {max_wait_s}s)...")
            return fetch_ola_xlsx_from_gmail(
                download_dir=DOWNLOAD_DIR,
                logger=lgr,
                poll_interval_s=20,
                max_wait_s=max_wait_s,
                lookback_minutes=lookback_minutes,
            )

        # ── Helper: open dropdown and select a filter ──────────────────────
        def _open_filter_dropdown(pg, lgr):
            """Click whichever filter is currently active to reveal the dropdown list."""
            for label in ["Today", "Yesterday", "Last 7 Days", "Last 30 Days",
                          "Custom Date", "Last 7 days"]:
                try:
                    btn = pg.locator(f"text={label}").first
                    if btn.is_visible(timeout=1500):
                        btn.click()
                        lgr(f"[FETCH] Opened filter dropdown (was: '{label}')")
                        pg.wait_for_timeout(1200)
                        return
                except Exception:
                    pass

        # ── Helper: apply "Custom Date" with target date window ──────
        def _select_custom_date_today(pg, lgr):
            lgr(f"[FETCH] Selecting Custom Date → {from_date.strftime('%Y-%m-%d')} to {to_date.strftime('%Y-%m-%d')}")
            _open_filter_dropdown(pg, lgr)
            pg.wait_for_timeout(800)

            # Click "Custom Date" option
            try:
                pg.locator("text=Custom Date").first.click(timeout=4000)
                lgr("[FETCH] Clicked 'Custom Date'")
            except Exception as _e:
                lgr(f"[FETCH] 'Custom Date' click error: {_e}")
            pg.wait_for_timeout(2500)
            ss(pg, "step8_custom_date_open", lgr)

            # Fill FROM date picker
            select_date_vuetify(pg, from_date, is_from=True,  logger=lgr)
            ss(pg, "step8_from_set", lgr)

            # Fill TO date picker
            select_date_vuetify(pg, to_date, is_from=False, logger=lgr)
            ss(pg, "step8_to_set", lgr)
            pg.wait_for_timeout(1500)

        # ── Helper: apply "Yesterday" quick-filter ───────────────────────
        def _select_yesterday_filter(pg, lgr):
            lgr("[FETCH] Selecting 'Yesterday' preset quick-filter...")
            _open_filter_dropdown(pg, lgr)
            pg.wait_for_timeout(800)
            try:
                pg.locator("text=Yesterday").first.click(timeout=4000)
                lgr("[FETCH] ✓ Selected 'Yesterday'")
            except Exception as _e:
                lgr(f"[FETCH] 'Yesterday' filter click error: {_e}")
            pg.wait_for_timeout(2000)
            ss(pg, "step8_yesterday_filter", lgr)

        # ── Helper: apply "Last 7 Days" quick-filter ─────────────────────
        def _select_last_7_days_filter(pg, lgr):
            lgr("[FETCH] Selecting 'Last 7 Days' preset quick-filter...")
            _open_filter_dropdown(pg, lgr)
            pg.wait_for_timeout(800)
            try:
                pg.locator("text=Last 7 Days").first.click(timeout=4000)
                lgr("[FETCH] ✓ Selected 'Last 7 Days'")
            except Exception as _e:
                lgr(f"[FETCH] 'Last 7 Days' filter click error: {_e}")
            pg.wait_for_timeout(2000)
            ss(pg, "step8_last_7_days_filter", lgr)

        # ── Helper: apply "Today" quick-filter ───────────────────────────
        def _select_today_filter(pg, lgr):
            lgr("[FETCH] Selecting 'Today' quick-filter...")
            _open_filter_dropdown(pg, lgr)
            pg.wait_for_timeout(800)
            try:
                pg.locator("text=Today").first.click(timeout=4000)
                lgr("[FETCH] ✓ Selected 'Today'")
            except Exception as _e:
                lgr(f"[FETCH] 'Today' filter click error: {_e}")
            pg.wait_for_timeout(2000)
            ss(pg, "step8_today_filter", lgr)

        # ── Helper: click DOWNLOAD STATEMENT and handle result ────────────
        def _attempt_download(pg, attempt_label: str, lgr,
                              attempt_start_ts: float) -> Optional[str]:
            """
            Click DOWNLOAD STATEMENT and capture either direct download or email modal.
            """
            lgr(f"[FETCH] [{attempt_label}] Clicking DOWNLOAD STATEMENT...")
            pg.wait_for_timeout(2000)
            ss(pg, f"pre_download_{attempt_label}", lgr)

            # Dismiss any active datepicker dialogs/overlays before clicking DOWNLOAD
            try:
                pg.keyboard.press("Escape")
                pg.wait_for_timeout(500)
            except Exception:
                pass

            # Pre-click check for email modal
            if _handle_email_modal(pg, EMAIL, lgr):
                lgr(f"[FETCH] [{attempt_label}] Email export triggered (pre-click) → polling IMAP...")
                return _poll_imap(lgr, lookback_minutes=45, max_wait_s=2400)

            # Set up direct download listener
            download_holder = []
            def on_download(download):
                download_holder.append(download)

            pg.on("download", on_download)

            try:
                pg.locator("text=DOWNLOAD STATEMENT").first.click(timeout=8000)
                lgr(f"[FETCH] [{attempt_label}] Clicked DOWNLOAD STATEMENT")
            except Exception as _e:
                lgr(f"[FETCH] [{attempt_label}] Download button click error: {_e}")
                return None

            pg.wait_for_timeout(2500)
            ss(pg, f"post_download_{attempt_label}", lgr)

            # Check if Ola error toast "Failed to download the data. Please try again." appeared
            try:
                err_toast = pg.locator("text=Failed to download the data").first
                if err_toast.is_visible(timeout=1500):
                    lgr(f"[FETCH] [{attempt_label}] ⚠️ Detected Ola error toast ('Failed to download'). Retrying burst click in 2s...")
                    pg.wait_for_timeout(2000)
                    pg.locator("text=DOWNLOAD STATEMENT").first.click(timeout=5000)
                    lgr(f"[FETCH] [{attempt_label}] Fired Burst 2 click on DOWNLOAD STATEMENT")
                    pg.wait_for_timeout(2500)
            except Exception:
                pass

            # Helper for timed 3-burst email submission & IMAP polling (with 12:01 PM burst support)
            def _poll_with_timed_burst(burst_wait_s: int = 600, total_s: int = 2400) -> Optional[str]:
                lgr(f"[FETCH] [{attempt_label}] 🚀 Burst 1 submitted → Polling IMAP for up to {burst_wait_s // 60} minutes...")
                res1 = _poll_imap(lgr, lookback_minutes=45, max_wait_s=burst_wait_s)
                if res1:
                    lgr(f"[FETCH] [{attempt_label}] ✓ Statement captured on Burst 1 ({res1})")
                    return res1

                # Burst 1 timed out -> Fire Burst 2 on Ola portal
                lgr(f"[FETCH] [{attempt_label}] ⚡ No email within {burst_wait_s // 60} mins. Re-triggering Burst 2 on Ola portal...")
                try:
                    pg.locator("text=DOWNLOAD STATEMENT").first.click(timeout=6000)
                    pg.wait_for_timeout(2000)
                    _handle_email_modal(pg, EMAIL, lgr)
                except Exception as _be:
                    lgr(f"[FETCH] [{attempt_label}] Burst 2 submission warning: {_be}")

                # Poll for up to 14 minutes before firing Burst 3 (at ~12:01 PM in Attempt 2)
                burst3_wait_s = 840 # 14 minutes
                lgr(f"[FETCH] [{attempt_label}] 🚀 Burst 2 submitted → Polling IMAP for up to {burst3_wait_s // 60} minutes...")
                res2 = _poll_imap(lgr, lookback_minutes=45, max_wait_s=burst3_wait_s)
                if res2:
                    lgr(f"[FETCH] [{attempt_label}] ✓ Statement captured on Burst 2 ({res2})")
                    return res2

                # Burst 2 timed out -> Fire Burst 3 on Ola portal (12:01 PM push)
                lgr(f"[FETCH] [{attempt_label}] ⚡ No email after 14 mins. Re-triggering Burst 3 on Ola portal (12:01 PM push)...")
                try:
                    pg.locator("text=DOWNLOAD STATEMENT").first.click(timeout=6000)
                    pg.wait_for_timeout(2000)
                    _handle_email_modal(pg, EMAIL, lgr)
                except Exception as _be3:
                    lgr(f"[FETCH] [{attempt_label}] Burst 3 submission warning: {_be3}")

                rem_s = max(total_s - burst_wait_s - burst3_wait_s, 600)
                lgr(f"[FETCH] [{attempt_label}] 🚀 Burst 3 submitted → Polling IMAP for remaining {rem_s // 60} minutes...")
                return _poll_imap(lgr, lookback_minutes=45, max_wait_s=rem_s)

            # Check if email modal appeared right after clicking DOWNLOAD STATEMENT
            if _handle_email_modal(pg, EMAIL, lgr):
                lgr(f"[FETCH] [{attempt_label}] Email export triggered (post-click) → starting timed burst polling...")
                return _poll_with_timed_burst(burst_wait_s=600, total_s=2400)

            # Dismiss OKAY/CONFIRM popup if present
            for popup_text in ["OKAY", "Okay", "OK", "CONFIRM", "Confirm"]:
                try:
                    btn = pg.locator(
                        f"button:has-text('{popup_text}'), "
                        f".v-dialog button:has-text('{popup_text}')"
                    ).first
                    if btn.is_visible(timeout=2000):
                        lgr(f"[FETCH] [{attempt_label}] Dismissing '{popup_text}' popup...")
                        btn.click()
                        pg.wait_for_timeout(1000)
                        break
                except Exception:
                    pass

            # Check again if email modal appeared after dismissing okay
            if _handle_email_modal(pg, EMAIL, lgr):
                lgr(f"[FETCH] [{attempt_label}] Email export triggered (post-okay) → starting timed burst polling...")
                return _poll_with_timed_burst(burst_wait_s=600, total_s=2400)

            # Wait briefly for direct browser download event
            wait_start = time.time()
            while time.time() - wait_start < 10:
                if download_holder:
                    download = download_holder[0]
                    date_str = today.strftime('%Y-%m-%d')
                    fname = f"ola_statement_{date_str}.xlsx"
                    save_path = os.path.join(DOWNLOAD_DIR, fname)
                    download.save_as(save_path)
                    _cleanup_temp_downloads(lgr)
                    lgr(f"[FETCH] [{attempt_label}] ✓ Direct browser download captured → {save_path}")
                    return save_path
                time.sleep(1)

            # Direct check for downloaded file in folder
            result = _check_direct_download(attempt_start_ts, lgr)
            if result:
                lgr(f"[FETCH] [{attempt_label}] ✓ Found file via direct directory check: {result}")
                return result

            # Final check: poll IMAP as fallback
            lgr(f"[FETCH] [{attempt_label}] Direct download not captured — polling IMAP fallback...")
            return _poll_imap(lgr, lookback_minutes=45, max_wait_s=600)

        # ════════════════════════════════════════════════════════════════════
        # DYNAMIC RETRY & PRESET SEQUENCE
        # ════════════════════════════════════════════════════════════════════
        # Clean up any leftover UUID temp files before starting
        _cleanup_temp_downloads(logger)

        yesterday_date = (datetime.today() - timedelta(days=1)).date()
        is_single_yesterday = (from_date.date() == yesterday_date and to_date.date() == yesterday_date)
        is_7_days = ((to_date.date() - from_date.date()).days == 6)

        if is_single_yesterday:
            logger("[FETCH] Target is single-day Yesterday -> Prioritizing 'Yesterday' native preset.")
            attempt_sequence = [
                ("attempt_1_yesterday_preset", _select_yesterday_filter),
                ("attempt_2_custom_date",      _select_custom_date_today),
                ("fallback_1_yesterday_burst", _select_yesterday_filter),
            ]
        else:
            logger(f"[FETCH] Multi-day target ({from_date.strftime('%Y-%m-%d')} to {to_date.strftime('%Y-%m-%d')}) -> Using exact Custom Date picker.")
            attempt_sequence = [
                ("attempt_1_custom_date",      _select_custom_date_today),
                ("attempt_2_custom_date",      _select_custom_date_today),
                ("fallback_1_custom_date",     _select_custom_date_today),
            ]

        saved_path = None

        for seq_idx, (attempt_label, filter_fn) in enumerate(attempt_sequence):

            # Before retry (not the first attempt): check IMAP for an already-
            # arrived email from a previous trigger (within 60 min of start).
            if seq_idx > 0:
                logger(f"[FETCH] [{attempt_label}] Pre-retry: checking IMAP for any already-arrived report...")
                imap_precheck = _poll_imap(logger, lookback_minutes=30, max_wait_s=30)
                if imap_precheck:
                    logger(f"[FETCH] ✓ Found existing email report before retry: {imap_precheck}")
                    saved_path = imap_precheck
                    break

            logger(f"\n[FETCH] ── Download {attempt_label.upper()} ──")
            attempt_ts = time.time()

            # Apply filter selection
            try:
                filter_fn(page, logger)
            except Exception as _fe:
                logger(f"[FETCH] [{attempt_label}] Filter selection error: {_fe}")

            # Attempt download
            result = _attempt_download(page, attempt_label, logger, attempt_ts)
            if result:
                saved_path = result
                logger(f"[FETCH] ✓ [{attempt_label}] Download succeeded: {result}")
                break
            else:
                logger(f"[FETCH] ✗ [{attempt_label}] Failed. {'Trying next approach...' if seq_idx < len(attempt_sequence) - 1 else 'All attempts exhausted.'}")
                # Brief pause between attempts to let the portal recover
                if seq_idx < len(attempt_sequence) - 1:
                    time.sleep(5)

        context.close()

        if not saved_path:
            raise RuntimeError(
                f"Download failed after all {len(attempt_sequence)} attempts. "
                "No direct download and no email report received."
            )


    return saved_path



# ── CLI entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    yesterday = datetime.today() - timedelta(days=1)
    from_date = yesterday
    to_date = yesterday

    # Create a preliminary log row
    log_id = None
    try:
        log_id = create_import_log_row(
            import_type="fetch",
            week_start=from_date.date(),
            week_end=to_date.date(),
            target_table="july_ola_raw",
        )
    except Exception as db_err:
        print(f"[WARN] Could not write to ola_import_log: {db_err}")

    try:
        path = fetch_ola_statement(log_id=log_id, logger=print)
        print(f"\n[OK] Download complete: {path}")
        if log_id:
            update_import_log(log_id, "Downloaded", file_name=os.path.basename(path))
    except Exception as e:
        print(f"\n[ERROR] {e}")
        traceback.print_exc()
        if log_id:
            try:
                update_import_log(log_id, "Failed", error_message=str(e))
            except Exception:
                pass
        sys.exit(1)
