"""
alerts.py
=========
Email Alerting Utility for LetzRyd Ola Ingestion Pipeline.
Uses Gmail SMTP with App Password to dispatch critical failure alerts.
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

def send_failure_alert(
    date_range_start: str,
    date_range_end: str,
    error_message: str,
    recipient_email: str = "vendor_aayush@letzryd.com",
    logger=print
) -> bool:
    """
    Sends an immediate HTML alert email when statement download/ingestion fails.
    """
    smtp_user = os.environ.get("GMAIL_IMAP_USER", os.environ.get("SMTP_USER", "vendor_aayush@letzryd.com"))
    smtp_pass = os.environ.get("GMAIL_IMAP_PASSWORD", os.environ.get("SMTP_PASSWORD", ""))

    if not smtp_pass:
        logger("[ALERT] ⚠️ Cannot send failure alert email: GMAIL_IMAP_PASSWORD / SMTP_PASSWORD not set.")
        return False

    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p IST")
    subject = f"⚠️ [ALERT] LetzRyd Ola Statement Ingestion Failed ({date_range_start} to {date_range_end})"

    html_content = f"""
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
                <td style="padding: 8px 0; font-weight: bold;">System Status:</td>
                <td style="padding: 8px 0;">Attempt sequence completed. Data safely preserved; no corrupted records written.</td>
            </tr>
        </table>

        <div style="background-color: #e2e3e5; padding: 12px; border-radius: 4px; font-size: 13px; color: #383d41;">
            <strong>Next Action:</strong> The self-healing cumulative pipeline will automatically attempt to backfill and ingest this date window during tomorrow's scheduled run.
        </div>
    </body>
    </html>
    """

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"LetzRyd Automation <{smtp_user}>"
        msg["To"] = recipient_email
        msg.attach(MIMEText(html_content, "html"))

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient_email], msg.as_string())

        logger(f"[ALERT] ✉️ Failure alert email successfully dispatched to {recipient_email}!")
        return True
    except Exception as e:
        logger(f"[ALERT] [!] Failed to send alert email: {e}")
        return False

if __name__ == "__main__":
    send_failure_alert("2026-08-24", "2026-08-24", "Test failure alert: Statement email not delivered by Ola.")
