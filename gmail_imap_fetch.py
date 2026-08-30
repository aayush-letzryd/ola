"""
gmail_imap_fetch.py
────────────────────────────────────────────────────────────
Poll vendor_aayush@letzryd.com via IMAP for a recent Ola
statement email with an .xlsx attachment.

Called when GCP cannot do a direct download from the Ola portal
and instead triggers an email export to vendor_aayush@letzryd.com.

Requirements:
  - Python stdlib only (imaplib, email, os, time)
  - Gmail IMAP must be enabled on the account:
      Gmail → Settings → See all settings → Forwarding and POP/IMAP → Enable IMAP
  - A Gmail App Password must be generated (not the normal login password):
      Google Account → Security → 2-Step Verification → App Passwords
      Set env vars: GMAIL_IMAP_USER and GMAIL_IMAP_PASSWORD

Usage:
  from gmail_imap_fetch import fetch_ola_xlsx_from_gmail
  path = fetch_ola_xlsx_from_gmail(download_dir="/app/ola-sync/ola_downloads", logger=print)
"""

import imaplib
import email
import os
import sys
import time
from email.header import decode_header
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Union

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

# ── Load .env ─────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if not _env_path.exists():
    _env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    with open(_env_path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())



IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Ola email subject keywords and sender patterns
OLA_SUBJECT_KEYWORDS = ["statement", "hisaab", "accounting", "settlement", "report", "details"]
OLA_SENDER_KEYWORDS  = ["olacabs.com", "@olacabs", "no-reply@olacabs.com", "noreply@olacabs.com", "olacabs"]


def _decode_str(value: Union[bytes, str]) -> str:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except Exception:
            return value.decode("latin-1", errors="replace")
    return value or ""


def fetch_ola_xlsx_from_gmail(
    download_dir: str,
    logger=print,
    poll_interval_s: int = 60,
    max_wait_s: int = 2400, # 40 minutes (10:35 AM to 11:15 AM)
    lookback_minutes: int = 45,
) -> Optional[str]:
    """
    Connect to Gmail via IMAP and download the most recent Ola statement .xlsx
    attachment received within the last `lookback_minutes` minutes.

    Parameters
    ----------
    download_dir      : Where to save the downloaded .xlsx file.
    logger            : Callable for logging (default: print).
    poll_interval_s   : Seconds between inbox polls (default: 30s).
    max_wait_s        : Maximum total wait time in seconds (default: 300s = 5 min).
    lookback_minutes  : Only consider emails received within this window (default: 20min).

    Returns
    -------
    Absolute path to saved .xlsx file, or None if not found within max_wait_s.
    """
    imap_user = os.environ.get("GMAIL_IMAP_USER", os.environ.get("SMTP_USER", "vendor_aayush@letzryd.com"))
    imap_pass = os.environ.get("GMAIL_IMAP_PASSWORD", os.environ.get("SMTP_PASSWORD", ""))

    if not imap_pass:
        logger("[GMAIL] ⚠️  Neither GMAIL_IMAP_PASSWORD nor SMTP_PASSWORD set. Cannot poll Gmail.")
        return None

    os.makedirs(download_dir, exist_ok=True)
    deadline = time.time() + max_wait_s

    logger(f"[GMAIL] Polling {imap_user} for Ola statement email (timeout: {max_wait_s}s)...")

    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            saved = _check_gmail_for_xlsx(imap_user, imap_pass, download_dir, lookback_minutes, logger)
            if saved:
                return saved
        except Exception as e:
            logger(f"[GMAIL] Poll attempt {attempt} error: {e}")

        remaining = int(deadline - time.time())
        if remaining <= 0:
            break
        wait = min(poll_interval_s, remaining)
        logger(f"[GMAIL] No Ola email yet. Waiting {wait}s (remaining: {remaining}s)...")
        time.sleep(wait)

    logger(f"[GMAIL] ⚠️  No Ola statement email found within {max_wait_s}s.")
    return None


def _check_gmail_for_xlsx(
    imap_user: str,
    imap_pass: str,
    download_dir: str,
    lookback_minutes: int,
    logger,
) -> Optional[str]:
    """
    Single IMAP connection attempt. Returns saved file path or None.

    NOTE: IMAP SINCE filter only works at day granularity (date only, NOT time).
    We enforce the actual lookback_minutes window by parsing each email's Date
    header and rejecting emails older than lookback_minutes from now.
    """
    since_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    # IMAP SINCE date format: DD-Mon-YYYY
    since_str = since_dt.strftime("%d-%b-%Y")

    with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT) as mail:
        mail.login(imap_user, imap_pass)
        mail.select("INBOX")

        # Search for recent emails since lookback date
        status, msg_ids_raw = mail.search(None, f'(SINCE "{since_str}")')
        if status != "OK" or not msg_ids_raw[0]:
            return None

        msg_ids = msg_ids_raw[0].split()
        # Process newest-first
        for msg_id in reversed(msg_ids):
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                # Decode subject and sender
                subject_raw = decode_header(msg.get("Subject", ""))[0]
                subject = _decode_str(subject_raw[0] if subject_raw[0] else b"").lower()
                sender  = msg.get("From", "").lower()
                # Ignore Uber automation emails
                if "uber" in subject or "uber" in sender:
                    continue

                # Check if this looks like an Ola statement email
                subject_match = any(kw in subject for kw in OLA_SUBJECT_KEYWORDS)
                sender_match  = any(kw in sender for kw in OLA_SENDER_KEYWORDS)

                if not (subject_match or sender_match):
                    continue

                logger(f"[GMAIL] Found candidate email — From: {sender} | Subject: {subject[:60]} | Date: {date_str}")

                # ── STRICT TIME FILTER ─────────────────────────────────────────
                # IMAP SINCE only filters by calendar day, NOT by time of day.
                # e.g. at 6:40 PM with lookback=30min, SINCE="10-Aug-2026" still
                # returns emails from 6:47 AM of the same day. Reject those here.
                try:
                    from email.utils import parsedate_to_datetime
                    email_dt = parsedate_to_datetime(date_str)
                    if email_dt.tzinfo is None:
                        email_dt = email_dt.replace(tzinfo=timezone.utc)
                    email_age_minutes = (datetime.now(timezone.utc) - email_dt).total_seconds() / 60
                    if email_age_minutes > lookback_minutes:
                        logger(
                            f"[GMAIL] ⏩ Skipping — email is {email_age_minutes:.1f} min old "
                            f"(limit: {lookback_minutes} min): {subject[:50]}"
                        )
                        continue
                    logger(f"[GMAIL] ✓ Email is {email_age_minutes:.1f} min old — within {lookback_minutes} min window")
                except Exception as e:
                    logger(f"[GMAIL] ⚠️  Could not parse email date '{date_str}': {e} — skipping to be safe")
                    continue
                # ──────────────────────────────────────────────────────────────

                # Walk MIME parts for .xlsx attachment
                for part in msg.walk():
                    content_type = part.get_content_type()
                    content_disp  = str(part.get("Content-Disposition", ""))

                    # Accept Excel files by content-type or disposition filename
                    filename = part.get_filename()
                    if filename:
                        filename = _decode_str(decode_header(filename)[0][0] if decode_header(filename)[0][0] else filename)

                    is_excel = (
                        "spreadsheetml" in content_type
                        or "octet-stream" in content_type
                        or (filename and (filename.lower().endswith(".xlsx") or filename.lower().endswith(".csv")))
                    )

                    if is_excel and filename:
                        logger(f"[GMAIL] Found valid statement attachment: {filename}")
                        payload = part.get_payload(decode=True)
                        if not payload or len(payload) < 5000:
                            continue  # skip empty/tiny files

                        today_str = datetime.now().strftime("%Y-%m-%d")
                        safe_fname = f"ola_statement_{today_str}.xlsx"
                        save_path = os.path.join(download_dir, safe_fname)
                        with open(save_path, "wb") as f:
                            f.write(payload)

                        logger(f"[GMAIL] ✅ Downloaded attachment: {safe_fname} ({len(payload):,} bytes) → {save_path}")
                        return save_path

            except Exception as e:
                logger(f"[GMAIL] Error processing message {msg_id}: {e}")
                continue

    return None


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    download_dir = sys.argv[1] if len(sys.argv) > 1 else "./ola_downloads"
    result = fetch_ola_xlsx_from_gmail(
        download_dir=download_dir,
        logger=print,
        poll_interval_s=15,
        max_wait_s=120,
        lookback_minutes=30,
    )
    if result:
        print(f"\n✅ File ready for parsing: {result}")
    else:
        print("\n❌ No Ola statement email found.")
