"""
alerts.py
=========
Email Notification & Alerting Utility for LetzRyd Ola Ingestion Pipeline.
Uses Gmail SMTP with App Password to dispatch status emails to vendor_aayush@letzryd.com.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
DEFAULT_RECIPIENT = "vendor_aayush@letzryd.com"

def _send_email(subject: str, html_body: str, recipient: str = DEFAULT_RECIPIENT, logger=print) -> bool:
    smtp_user = os.environ.get("GMAIL_IMAP_USER", os.environ.get("SMTP_USER", DEFAULT_RECIPIENT))
    smtp_pass = os.environ.get("GMAIL_IMAP_PASSWORD", os.environ.get("SMTP_PASSWORD", ""))

    if not smtp_pass:
        logger("[EMAIL] ⚠️ Cannot send notification: GMAIL_IMAP_PASSWORD / SMTP_PASSWORD not set.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"LetzRyd Ola Automation <{smtp_user}>"
        msg["To"] = recipient
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())

        logger(f"[EMAIL] ✉️ Notification successfully dispatched to {recipient}!")
        return True
    except Exception as e:
        logger(f"[EMAIL] [!] Failed to send notification email: {e}")
        return False

def send_success_notification(
    date_range_start: str,
    date_range_end: str,
    trips_count: int,
    txns_count: int,
    public_url: str = None,
    duration_seconds: float = 0.0,
    recipient_email: str = DEFAULT_RECIPIENT,
    logger=print
) -> bool:
    """Dispatches a green success summary email on successful ingestion."""
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p IST")
    subject = f"✅ [SUCCESS] LetzRyd Ola Statement Ingested ({date_range_start} to {date_range_end})"

    url_btn = ""
    if public_url:
        url_btn = f"""
        <div style="margin-top: 20px;">
            <a href="{public_url}" style="background-color: #28a745; color: white; padding: 10px 18px; text-decoration: none; border-radius: 4px; font-weight: bold; display: inline-block;">📥 Download Raw Statement (.xlsx)</a>
        </div>
        """

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="background-color: #d4edda; border: 1px solid #c3e6cb; padding: 15px; border-radius: 6px; margin-bottom: 20px;">
            <h2 style="color: #155724; margin: 0 0 10px 0;">✅ Ola Statement Ingestion Successful</h2>
            <p style="margin: 0; font-size: 14px;">The automated ingestion completed and all data was loaded into PostgreSQL.</p>
        </div>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold; width: 180px;">Target Date Window:</td>
                <td style="padding: 8px 0;">{date_range_start} &rarr; {date_range_end}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Timestamp:</td>
                <td style="padding: 8px 0;">{now_str}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Trips Ingested:</td>
                <td style="padding: 8px 0; font-weight: bold; color: #28a745;">{trips_count:,} rides</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Financial Ledger Rows:</td>
                <td style="padding: 8px 0; font-weight: bold; color: #28a745;">{txns_count:,} transactions</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Duration:</td>
                <td style="padding: 8px 0;">{duration_seconds:.1f} seconds</td>
            </tr>
        </table>

        {url_btn}
    </body>
    </html>
    """
    return _send_email(subject, html, recipient_email, logger=logger)

def send_audit_notification(
    week_start: str,
    week_end: str,
    trip_diffs: int,
    txn_diffs: int,
    public_url: str = None,
    recipient_email: str = DEFAULT_RECIPIENT,
    logger=print
) -> bool:
    """Dispatches an audit summary email for Tuesday Reconciliation."""
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p IST")
    subject = f"🔍 [AUDIT] LetzRyd Ola Tuesday Reconciliation ({week_start} to {week_end})"

    status_color = "#28a745" if (trip_diffs == 0 and txn_diffs == 0) else "#ffc107"
    status_text = "100% RECONCILED (0 Discrepancies)" if (trip_diffs == 0 and txn_diffs == 0) else f"Found {trip_diffs} Trip Diffs, {txn_diffs} Ledger Diffs"

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="background-color: #d1ecf1; border: 1px solid #bee5eb; padding: 15px; border-radius: 6px; margin-bottom: 20px;">
            <h2 style="color: #0c5460; margin: 0 0 10px 0;">🔍 Tuesday Reconciliation Audit Complete</h2>
            <p style="margin: 0; font-size: 14px;">Comparison completed between Monday baseline and Tuesday statement.</p>
        </div>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold; width: 180px;">Audited Week:</td>
                <td style="padding: 8px 0;">{week_start} &rarr; {week_end}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Audit Timestamp:</td>
                <td style="padding: 8px 0;">{now_str}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Audit Result:</td>
                <td style="padding: 8px 0; font-weight: bold; color: {status_color};">{status_text}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Trip Discrepancies:</td>
                <td style="padding: 8px 0;">{trip_diffs} rows in ola_audit_diff_crns</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Ledger Discrepancies:</td>
                <td style="padding: 8px 0;">{txn_diffs} rows in ola_audit_diff_transactions</td>
            </tr>
        </table>
    </body>
    </html>
    """
    return _send_email(subject, html, recipient_email, logger=logger)

def send_failure_alert(
    date_range_start: str,
    date_range_end: str,
    error_message: str,
    recipient_email: str = DEFAULT_RECIPIENT,
    logger=print
) -> bool:
    """Dispatches a red alert email when statement download/ingestion fails."""
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p IST")
    subject = f"⚠️ [FAILURE] LetzRyd Ola Ingestion Failed ({date_range_start} to {date_range_end})"

    html = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="background-color: #f8d7da; border: 1px solid #f5c6cb; padding: 15px; border-radius: 6px; margin-bottom: 20px;">
            <h2 style="color: #721c24; margin: 0 0 10px 0;">⚠️ Ola Pipeline Ingestion Alert</h2>
            <p style="margin: 0; font-size: 14px;">An automated statement ingestion run for LetzRyd was unable to complete.</p>
        </div>

        <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold; width: 180px;">Target Date Window:</td>
                <td style="padding: 8px 0;">{date_range_start} &rarr; {date_range_end}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Timestamp:</td>
                <td style="padding: 8px 0;">{now_str}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Failure Reason:</td>
                <td style="padding: 8px 0; color: #c82333;">{error_message}</td>
            </tr>
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 8px 0; font-weight: bold;">Database Status:</td>
                <td style="padding: 8px 0;">Previous records 100% safe; zero corrupted records written.</td>
            </tr>
        </table>

        <div style="background-color: #e2e3e5; padding: 12px; border-radius: 4px; font-size: 13px; color: #383d41;">
            <strong>Next Action:</strong> The self-healing cumulative pipeline will automatically attempt to backfill and ingest this date window during the next scheduled trigger.
        </div>
    </body>
    </html>
    """
    return _send_email(subject, html, recipient_email, logger=logger)
