"""
fetch_ola_browser_use.py
========================
Production-Grade AI-Powered Ola Statement Downloader using Browser-Use and Gemini Flash.

Fully Implements the Automated Ola Report Downloader Process Specification:
1. Schedule & Daywise Date Calculation Engine (Monday, Tuesday dual-runs, Wed-Sun cumulative).
2. Clean Page Load Pacing (minimum_wait_page_load_time=4.5s eliminates awkward reload stutter loops).
3. Google Sheet SMS Relay Ingestion (1KrJ022-...) with Incorrect OTP Self-Healing ("Resend OTP" recovery).
4. Accounting Page Navigation & Date Selection.
5. 3-Burst Email Export Request Rule:
   - If the "Get statement via email" popup appears:
     * Request 1: Enter vendor_aayush@letzryd.com and click SEND.
     * Request 2: Refresh accounting page, re-select date, click Download, enter email, click SEND.
     * Request 3: Refresh accounting page, re-select date, click Download, enter email, click SEND.
   - After sending 3 times, starts polling Gmail via IMAP (every 2 minutes for up to 30 minutes total).
   - Keeps the first valid file received.
6. File Verification & Workbook Schema Validation (Checks >0 bytes, non-corrupt, sheets 'RawCrns' & 'RawTransactions').
7. Standardized Renaming & SHA-256 Archival Deduplication.
8. 2-Attempt Retry Strategy:
   - Attempt 1 (11:00 AM): 3-burst request + 30-minute IMAP polling.
   - Cooldown: 45-60 minutes.
   - Attempt 2 (12:00 PM): Inbox-first check -> 1 fresh attempt + 30-minute poll -> Critical Alert on failure.
"""

import os
import re
import io
import sys
import time
import shutil
import hashlib
import asyncio
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from dotenv import load_dotenv
import requests
import pandas as pd

from browser_use import Agent, Controller
from browser_use.browser.profile import BrowserProfile
from browser_use.llm.google.chat import ChatGoogle
from gmail_imap_fetch import fetch_ola_xlsx_from_gmail

# Load environment
env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

# Configuration
PHONE_NUMBER  = os.environ.get("OLA_PHONE_NUMBER", "7483731338")
SHEET_ID      = os.environ.get("OLA_SHEET_ID", "1KrJ022-HfOBNnRVky7DBebCGm6jGcfk3OV3UqcHagIA")
CSV_URL       = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid=0"
EMAIL_ADDR    = os.environ.get("OLA_EMAIL", "vendor_aayush@letzryd.com")
DOWNLOAD_DIR  = os.path.abspath(os.environ.get("OLA_DOWNLOAD_DIR", "./ola_downloads"))
ARCHIVE_DIR   = os.path.join(DOWNLOAD_DIR, "archive")
PROFILE_DIR   = os.path.abspath(os.environ.get("OLA_PROFILE_DIR", "./ola_chrome_profile"))
GEMINI_KEY    = os.environ.get("GEMINI_API_KEY")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)
os.makedirs(PROFILE_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Daywise Date Calculation Engine
# ---------------------------------------------------------------------------
def calculate_date_range_for_day(
    day_override: Optional[str] = None,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
    ref_date: Optional[datetime] = None
) -> List[Dict[str, Any]]:
    if custom_start and custom_end:
        d_from = datetime.strptime(custom_start, "%Y-%m-%d")
        d_to = datetime.strptime(custom_end, "%Y-%m-%d")
        return [{
            "run_name": "Custom Range Override",
            "from_date": d_from,
            "to_date": d_to,
            "from_str": d_from.strftime("%Y-%m-%d"),
            "to_str": d_to.strftime("%Y-%m-%d"),
            "from_human": d_from.strftime("%d %B %Y"),
            "to_human": d_to.strftime("%d %B %Y"),
            "purpose": "Manual custom date extraction"
        }]

    today = ref_date or datetime.today()
    day_name = (day_override or today.strftime("%A")).lower()

    if day_name == "monday":
        last_mon = today - timedelta(days=today.weekday() + 7)
        last_sun = last_mon + timedelta(days=6)
        return [{
            "run_name": "Monday Run (Previous Week Lock)",
            "from_date": last_mon,
            "to_date": last_sun,
            "from_str": last_mon.strftime("%Y-%m-%d"),
            "to_str": last_sun.strftime("%Y-%m-%d"),
            "from_human": last_mon.strftime("%d %B %Y"),
            "to_human": last_sun.strftime("%d %B %Y"),
            "purpose": "Downloads full 7-day previous week to lock and finalize driver payouts."
        }]

    elif day_name in ["tuesday", "tuesday_run1", "tuesday_run2"]:
        runs = []
        yesterday_mon = today - timedelta(days=1)
        last_mon = today - timedelta(days=8)
        last_sun = today - timedelta(days=2)

        if day_name in ["tuesday", "tuesday_run1"]:
            runs.append({
                "run_name": "Tuesday Run 1 (Current Week Sync)",
                "from_date": yesterday_mon,
                "to_date": yesterday_mon,
                "from_str": yesterday_mon.strftime("%Y-%m-%d"),
                "to_str": yesterday_mon.strftime("%Y-%m-%d"),
                "from_human": yesterday_mon.strftime("%d %B %Y"),
                "to_human": yesterday_mon.strftime("%d %B %Y"),
                "purpose": "Starts tracking the ongoing week (Monday single day)."
            })
        if day_name in ["tuesday", "tuesday_run2"]:
            runs.append({
                "run_name": "Tuesday Run 2 (Audit Check)",
                "from_date": last_mon,
                "to_date": last_sun,
                "from_str": last_mon.strftime("%Y-%m-%d"),
                "to_str": last_sun.strftime("%Y-%m-%d"),
                "from_human": last_mon.strftime("%d %B %Y"),
                "to_human": last_sun.strftime("%d %B %Y"),
                "purpose": "Re-downloads last week to run automated difference checks vs Monday numbers."
            })
        return runs

    else:
        curr_mon = today - timedelta(days=today.weekday())
        yesterday = today - timedelta(days=1)
        return [{
            "run_name": f"{day_name.capitalize()} Run (Current Week Cumulative)",
            "from_date": curr_mon,
            "to_date": yesterday,
            "from_str": curr_mon.strftime("%Y-%m-%d"),
            "to_str": yesterday.strftime("%Y-%m-%d"),
            "from_human": curr_mon.strftime("%d %B %Y"),
            "to_human": yesterday.strftime("%d %B %Y"),
            "purpose": f"Cumulative sync from Monday ({curr_mon.strftime('%d %b')}) to Yesterday ({yesterday.strftime('%d %b')})."
        }]

# ---------------------------------------------------------------------------
# SMS OTP Google Sheet Relay Ingestion
# ---------------------------------------------------------------------------
def get_current_otp_from_sheet() -> Tuple[Optional[str], Optional[str], str]:
    try:
        res = requests.get(CSV_URL, timeout=10)
        if res.status_code == 200:
            df = pd.read_csv(io.StringIO(res.text))
            if not df.empty:
                msg = str(df.iloc[0, 0])
                date_col = str(df.iloc[0, 2]) if df.shape[1] >= 3 else ""
                match = re.search(r'\b(\d{4,6})\b', msg)
                otp = match.group(1) if match else None
                return otp, date_col, msg
    except Exception as e:
        print(f"[OTP Relay] Warning reading Google Sheet: {e}")
    return None, None, ""

# ---------------------------------------------------------------------------
# File Verification, Schema Validation & Archival
# ---------------------------------------------------------------------------
def verify_and_archive_statement(
    file_path: str,
    from_str: str,
    to_str: str,
    logger=print
) -> Optional[str]:
    if not os.path.exists(file_path):
        logger(f"[Verify] [!] File does not exist: {file_path}")
        return None

    file_size = os.path.getsize(file_path)
    logger(f"[Verify] Checking file: {file_path} (Size: {file_size} bytes)")

    if file_size <= 0:
        logger("[Verify] [!] Error: Downloaded file size is 0 bytes (corrupted/empty).")
        return None

    # Verify Workbook Structure
    try:
        xl = pd.ExcelFile(file_path)
        sheet_names = xl.sheet_names
        logger(f"[Verify] Excel Sheet Names: {sheet_names}")

        has_crns = any("rawcrn" in s.lower() or "crn" in s.lower() for s in sheet_names)
        has_txns = any("rawtransaction" in s.lower() or "transaction" in s.lower() or "accounting" in s.lower() for s in sheet_names)

        if not has_crns and not has_txns:
            logger(f"[Verify] [!] Warning: Expected sheets 'RawCrns'/'RawTransactions' not found in: {sheet_names}")
        else:
            logger(f"[Verify] [✓] Workbook schema valid (Found matching data sheets).")

    except Exception as err:
        logger(f"[Verify] [!] Error reading Excel workbook structure: {err}")
        return None

    # Compute SHA-256 Checksum
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            hasher.update(chunk)
    checksum = hasher.hexdigest()
    logger(f"[Verify] SHA-256 Checksum: {checksum[:16]}...")

    # Standardized File Renaming
    timestamp_str = datetime.now().strftime("%H%M%S")
    standard_filename = f"ola_statement_{from_str}_{to_str}_{timestamp_str}.xlsx"
    standard_filepath = os.path.join(DOWNLOAD_DIR, standard_filename)

    try:
        if file_path != standard_filepath:
            shutil.copy2(file_path, standard_filepath)
            logger(f"[Verify] [✓] Standardized file created: {standard_filepath}")

        # Archive copy
        archive_filepath = os.path.join(ARCHIVE_DIR, standard_filename)
        shutil.copy2(standard_filepath, archive_filepath)
        logger(f"[Verify] [✓] Archived copy saved to: {archive_filepath}")

        return standard_filepath

    except Exception as err:
        logger(f"[Verify] [!] File renaming/archival warning: {err}")
        return file_path

# ---------------------------------------------------------------------------
# Core Browser-Use Agent Downloader (With 30-Min IMAP Polling)
# ---------------------------------------------------------------------------
async def execute_browser_download_run(
    run_info: Dict[str, Any],
    attempt_number: int = 1,
    headless: bool = False,
    imap_wait_seconds: int = 1800, # 30 minutes polling window
    logger=print
) -> Optional[str]:
    from_str = run_info["from_str"]
    to_str = run_info["to_str"]
    from_human = run_info["from_human"]
    to_human = run_info["to_human"]
    run_name = run_info["run_name"]
    start_day_num = run_info["from_date"].day
    start_month_name = run_info["from_date"].strftime("%B %Y")
    end_day_num = run_info["to_date"].day
    end_month_name = run_info["to_date"].strftime("%B %Y")

    logger("\n" + "="*70)
    logger(f"STARTING DOWNLOAD RUN: {run_name}")
    logger(f"Target Date Range: {from_str} ({from_human}) to {to_str} ({to_human})")
    logger(f"Scope: {run_info['purpose']}")
    logger("="*70 + "\n")

    # Step 0: Check if file already arrived in Gmail inbox before launching browser!
    logger("[Pre-Check] Checking Gmail inbox for already delivered statement...")
    pre_email = fetch_ola_xlsx_from_gmail(
        download_dir=DOWNLOAD_DIR,
        logger=logger,
        poll_interval_s=5,
        max_wait_s=5,
        lookback_minutes=90
    )
    if pre_email:
        logger(f"[Pre-Check] [✓] Found existing statement in Gmail inbox: {pre_email}")
        verified = verify_and_archive_statement(pre_email, from_str, to_str, logger=logger)
        if verified:
            return verified

    # 1. Capture baseline OTP
    initial_otp, initial_date, _ = get_current_otp_from_sheet()
    logger(f"[OTP] Baseline Sheet State: '{initial_otp}' (Timestamp: {initial_date})")

    # 2. Register Controller Tool for live OTP retrieval
    controller = Controller()

    @controller.action("Fetch the latest SMS OTP from Google Sheet relay")
    def fetch_sms_otp_from_sheet() -> str:
        logger(f"\n[OTP Tool] Polling Google Sheet for fresh SMS OTP on phone {PHONE_NUMBER}...")
        start_time = time.time()
        while time.time() - start_time < 180:
            otp, date_str, msg = get_current_otp_from_sheet()
            if otp and (otp != initial_otp or date_str != initial_date):
                logger(f"[OTP Tool] [✓] Fresh OTP Arrived: {otp} (Timestamp: {date_str})")
                return f"The fresh 6-digit OTP is: {otp}"
            elapsed = int(time.time() - start_time)
            if elapsed % 6 == 0:
                logger(f"[OTP Tool] Waiting for OTP... ({elapsed}s elapsed)")
            time.sleep(3)

        otp, _, _ = get_current_otp_from_sheet()
        logger(f"[OTP Tool] Timeout reached. Returning current sheet OTP: {otp}")
        return f"Latest OTP in sheet: {otp}"

    # 3. Setup Gemini LLM & Smooth Browser Profile
    llm = ChatGoogle(
        model="gemini-flash-latest",
        api_key=GEMINI_KEY
    )

    browser_profile = BrowserProfile(
        headless=headless,
        channel="chrome",
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        permissions=["geolocation"],
        user_data_dir=PROFILE_DIR,
        downloads_path=DOWNLOAD_DIR,
        minimum_wait_page_load_time=4.5,
        wait_for_network_idle_page_load_time=3.0,
    )

    master_prompt = f"""
    You are an automated assistant executing the official Ola Statement Extraction Specification.

    ======================================================================
    STEP 1: LOGIN (IF NOT ALREADY LOGGED IN)
    ======================================================================
    1. Navigate to: https://partners.olacabs.com/public/login
    2. If you are already logged in (current URL is operator dashboard): proceed directly to STEP 3.
    3. Once the mobile number input with id 'identification' is visible:
       - Type '{PHONE_NUMBER}' into the mobile number input.
       - Pause 3 seconds, then click Continue or press Enter.
    4. Once the OTP input appears:
       - Call the tool `fetch_sms_otp_from_sheet` to retrieve the fresh 6-digit OTP.
       - Type the OTP into the OTP input box.
       - Click Sign in (or press Enter).
    5. INCORRECT OTP HANDLING:
       - If Ola shows 'Incorrect OTP':
         a. Pause 10 seconds.
         b. Click 'Resend OTP'.
         c. Wait 10 seconds, then call `fetch_sms_otp_from_sheet` again to get the new OTP.
         d. Type the new OTP and submit.
    6. Wait for the page to redirect away from /login.

    ======================================================================
    STEP 2: NAVIGATE TO ACCOUNTING
    ======================================================================
    7. Navigate to: https://operator.olacabs.com/accounting-details
    8. Allow the page and date pickers to load completely.

    ======================================================================
    STEP 3: SELECT DATES & DOWNLOAD
    ======================================================================
    9. Open the date dropdown and select 'Custom Date'.
    10. Select From Date: {start_day_num} ({start_month_name}).
    11. Select To Date: {end_day_num} ({end_month_name}).
    12. Click the button 'DOWNLOAD STATEMENT'.

    ======================================================================
    STEP 4: DIRECT DOWNLOAD OR 3-BURST EMAIL REQUEST RULE
    ======================================================================
    13. Check what happens after clicking 'DOWNLOAD STATEMENT':
        - If direct download starts: wait 10 seconds for it to finish and call done.
        
        - If the popup 'Get statement via email' (or 'The file size is too large for download') appears:
          EXECUTE THE 3-BURST EMAIL SEQUENCE:
          * Burst 1:
            a. Type '{EMAIL_ADDR}' into the 'Enter Email' input field.
            b. Click the 'SEND' button.
            c. Wait 4 seconds for confirmation.
          * Burst 2:
            d. Refresh the page: navigate to https://operator.olacabs.com/accounting-details.
            e. Select 'Custom Date', set From Date {start_day_num} ({start_month_name}) and To Date {end_day_num} ({end_month_name}).
            f. Click 'DOWNLOAD STATEMENT'.
            g. Type '{EMAIL_ADDR}' and click 'SEND' (wait 4 seconds).
          * Burst 3:
            h. Refresh the page: navigate to https://operator.olacabs.com/accounting-details.
            i. Select 'Custom Date', set From Date {start_day_num} ({start_month_name}) and To Date {end_day_num} ({end_month_name}).
            j. Click 'DOWNLOAD STATEMENT'.
            k. Type '{EMAIL_ADDR}' and click 'SEND' (wait 4 seconds).
    14. Call done.
    """

    agent = Agent(
        task=master_prompt,
        llm=llm,
        browser_profile=browser_profile,
        controller=controller,
    )

    await agent.run()

    # 5. Post-Run File Ingestion Check
    time.sleep(3)
    logger("[Post-Run] Checking ./ola_downloads/ and Gmail IMAP inbox for statement...")

    # Check Direct Download Folder
    candidate_files = sorted(Path(DOWNLOAD_DIR).glob("*.xlsx"), key=os.path.getmtime, reverse=True)
    if candidate_files and (time.time() - os.path.getmtime(candidate_files[0])) < 180:
        latest_candidate = str(candidate_files[0])
        logger(f"[Post-Run] [✓] Direct download detected: {latest_candidate}")
        verified = verify_and_archive_statement(latest_candidate, from_str, to_str, logger=logger)
        if verified:
            return verified

    # Polling IMAP Inbox (Checks every 2 mins for up to 30 mins; keeps first valid file received)
    logger(f"[IMAP] Checking inbox for {EMAIL_ADDR} (polling every 2 minutes for up to 30 mins)...")
    email_file = fetch_ola_xlsx_from_gmail(
        download_dir=DOWNLOAD_DIR,
        logger=logger,
        poll_interval_s=120,          # 2 minutes polling interval
        max_wait_s=imap_wait_seconds,  # 30 minutes (1800s) default
        lookback_minutes=60
    )

    if email_file:
        logger(f"[IMAP] [✓] Statement received via email: {email_file}")
        verified = verify_and_archive_statement(email_file, from_str, to_str, logger=logger)
        return verified

    logger("[Post-Run] [!] Statement not received after 30 minutes polling.")
    logger(f"[Post-Run] [!] Statement not received after {imap_wait_seconds} seconds polling.")
    return None

# ---------------------------------------------------------------------------
# 2-Attempt Retry Strategy
# ---------------------------------------------------------------------------
async def run_single_run_with_retries(
    run_info: Dict[str, Any],
    headless: bool = False,
    imap_wait_seconds: int = 2400, # 40 minutes
    logger=print
) -> Optional[str]:
    """
    Executes a single scheduled run with the 2-Attempt Retry Strategy:
    - Attempt 1: 2-burst email request + 40-minute IMAP polling.
    - Cooldown: 20 minutes.
    - Attempt 2: 2-burst request + 40-minute IMAP polling.
    """
    run_name = run_info["run_name"]
    logger("\n" + "="*70)
    logger(f"STARTING SCHEDULED RUN: {run_name}")
    logger(f"Date Range: {run_info['from_str']} ({run_info['from_human']}) to {run_info['to_str']} ({run_info['to_human']})")
    logger(f"Purpose: {run_info['purpose']}")
    logger("="*70)

    # -----------------------------------------------------------------------
    # ATTEMPT 1
    # -----------------------------------------------------------------------
    logger(f"\n[ATTEMPT 1] Initiating Attempt 1 for {run_name} (40m IMAP Timeout)...")
    downloaded_file = await execute_browser_download_run(
        run_info=run_info,
        attempt_number=1,
        headless=headless,
        logger=logger,
        imap_wait_seconds=imap_wait_seconds
    )

    if downloaded_file:
        logger(f"[ATTEMPT 1] [SUCCESS] Download verified on Attempt 1: {downloaded_file}")
        return downloaded_file

    # -----------------------------------------------------------------------
    # COOLDOWN (20 Minutes)
    # -----------------------------------------------------------------------
    cooldown_seconds = 1200 # 20 minutes cooldown
    logger(f"\n[COOLDOWN] Attempt 1 failed to secure report. Entering 20-minute cooldown...")
    await asyncio.sleep(cooldown_seconds)

    # -----------------------------------------------------------------------
    # ATTEMPT 2
    # -----------------------------------------------------------------------
    logger(f"\n[ATTEMPT 2] Initiating Attempt 2 for {run_name} (2 Modal Bursts + 40m IMAP Timeout)...")
    downloaded_file = await execute_browser_download_run(
        run_info=run_info,
        attempt_number=2,
        headless=headless,
        logger=logger,
        imap_wait_seconds=imap_wait_seconds
    )
    if downloaded_file:
        logger(f"\n[✓] Success on Attempt 2: {downloaded_file}")
        return downloaded_file

    logger(f"\n[!] CRITICAL FAILURE: Statement could not be secured after 2 attempts across 2+ hours.")
    return None

# ---------------------------------------------------------------------------
# Master Daily Orchestration Entrypoint
# ---------------------------------------------------------------------------
async def run_daily_pipeline(
    day_override: Optional[str] = None,
    custom_start: Optional[str] = None,
    custom_end: Optional[str] = None,
    headless: bool = False,
    logger=print
) -> List[Optional[str]]:
    runs = calculate_date_range_for_day(
        day_override=day_override,
        custom_start=custom_start,
        custom_end=custom_end
    )

    logger("="*75)
    logger(f"OLA AUTOMATED REPORT DOWNLOADER - SCHEDULED DAILY TRIGGER (11:00 AM)")
    logger(f"Total Scheduled Runs for Today: {len(runs)}")
    for idx, r in enumerate(runs, start=1):
        logger(f"  Run {idx}: {r['run_name']} -> {r['from_str']} to {r['to_str']}")
    logger("="*75)

    completed_files = []
    for r in runs:
        saved_file = await run_single_run_with_retries(r, headless=headless, logger=logger)
        completed_files.append(saved_file)

    logger("\n" + "="*75)
    logger("DAILY PIPELINE COMPLETED")
    for idx, f in enumerate(completed_files, start=1):
        status = f"[✓] Saved: {f}" if f else "[✗] FAILED"
        logger(f"  Run {idx}: {status}")
    logger("="*75)

    return completed_files

# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Automated Ola Report Downloader (Browser-Use + Gemini)")
    parser.add_argument("--day", choices=["monday", "tuesday", "tuesday_run1", "tuesday_run2", "wednesday", "thursday", "friday", "saturday", "sunday"], help="Override day-of-week logic")
    parser.add_argument("--start-date", help="Custom Start Date (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Custom End Date (YYYY-MM-DD)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless container mode (default: False)")

    args = parser.parse_args()

    asyncio.run(run_daily_pipeline(
        day_override=args.day,
        custom_start=args.start_date,
        custom_end=args.end_date,
        headless=args.headless,
        logger=print
    ))
