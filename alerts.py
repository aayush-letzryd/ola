"""
alerts.py
=========
Executive-Grade Email Notification & Alerting Utility for LetzRyd Ola Pipeline.
Includes LetzRyd branded header/footer, inline logo, and color-coded status cards.
"""

import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
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
LOGO_PATH = Path(__file__).parent / "assets" / "letzryd_logo.png"

def _get_base_template(status_type: str, title: str, subtitle: str, content_table: str, action_cta: str = "") -> str:
    """Constructs an executive branded HTML email template with LetzRyd logo."""
    # Theme colors
    colors = {
        "SUCCESS": {"banner_bg": "#e6f4ea", "banner_border": "#34a853", "banner_text": "#137333", "badge": "STATUS: SUCCESSFUL", "badge_bg": "#34a853"},
        "AUDIT":   {"banner_bg": "#e8f0fe", "banner_border": "#1a73e8", "banner_text": "#174ea6", "badge": "TUESDAY RECONCILIATION", "badge_bg": "#1a73e8"},
        "FAILURE": {"banner_bg": "#fce8e6", "banner_border": "#ea4335", "banner_text": "#c5221f", "badge": "STATUS: ACTION REQUIRED", "badge_bg": "#ea4335"},
    }
    c = colors.get(status_type, colors["SUCCESS"])
    now_str = datetime.now().strftime("%d %b %Y, %I:%M %p IST")

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin: 0; padding: 0; background-color: #f4f6f8; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #202124;">
        <table width="100%" cellpadding="0" cellspacing="0" style="background-color: #f4f6f8; padding: 30px 10px;">
            <tr>
                <td align="center">
                    <table width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 10px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.06); border: 1px solid #e1e4e8;">
                        
                        <!-- Header Banner -->
                        <tr>
                            <td style="background-color: #ffffff; padding: 24px 30px 18px 30px; text-align: center; border-bottom: 1px solid #edf0f2;">
                                <img src="cid:letzryd_logo" alt="LetzRyd Logo" style="height: 52px; width: auto; display: block; margin: 0 auto 12px auto;" />
                                <div style="font-size: 11px; font-weight: 700; letter-spacing: 1.2px; color: #5f6368; text-transform: uppercase;">Fleet Financial Operations • Ola Pipeline</div>
                            </td>
                        </tr>

                        <!-- Status Badge & Title -->
                        <tr>
                            <td style="padding: 24px 30px 10px 30px;">
                                <div style="display: inline-block; background-color: {c['badge_bg']}; color: #ffffff; font-size: 11px; font-weight: 700; letter-spacing: 0.8px; padding: 4px 10px; border-radius: 20px; text-transform: uppercase; margin-bottom: 12px;">
                                    {c['badge']}
                                </div>
                                <h1 style="margin: 0 0 6px 0; font-size: 20px; font-weight: 700; color: #202124;">{title}</h1>
                                <p style="margin: 0; font-size: 13px; color: #5f6368;">{subtitle}</p>
                            </td>
                        </tr>

                        <!-- Main Content Details Table -->
                        <tr>
                            <td style="padding: 15px 30px 20px 30px;">
                                <div style="background-color: {c['banner_bg']}; border-left: 4px solid {c['banner_border']}; border-radius: 6px; padding: 18px 20px; margin-bottom: 20px;">
                                    {content_table}
                                </div>
                                {action_cta}
                            </td>
                        </tr>

                        <!-- Professional Footer -->
                        <tr>
                            <td style="background-color: #fafbfc; border-top: 1px solid #edf0f2; padding: 22px 30px; text-align: center;">
                                <img src="cid:letzryd_logo" alt="LetzRyd Logo" style="height: 28px; width: auto; opacity: 0.85; display: block; margin: 0 auto 8px auto;" />
                                <p style="margin: 0 0 4px 0; font-size: 12px; font-weight: 600; color: #3c4043;">LetzRyd Mobility Private Limited</p>
                                <p style="margin: 0 0 6px 0; font-size: 11px; color: #70757a;">Automated Cloud Pipeline • Serverless Microservice (asia-south1)</p>
                                <p style="margin: 0; font-size: 10px; color: #9aa0a6;">Execution Timestamp: {now_str} • Confidential</p>
                            </td>
                        </tr>

                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """

def _send_email(subject: str, html_body: str, recipient: str = DEFAULT_RECIPIENT, logger=print) -> bool:
    smtp_user = os.environ.get("GMAIL_IMAP_USER", os.environ.get("SMTP_USER", DEFAULT_RECIPIENT))
    smtp_pass = os.environ.get("GMAIL_IMAP_PASSWORD", os.environ.get("SMTP_PASSWORD", ""))

    if not smtp_pass:
        logger("[EMAIL] ⚠️ Cannot send notification: GMAIL_IMAP_PASSWORD / SMTP_PASSWORD not set.")
        return False

    try:
        msg = MIMEMultipart("related")
        msg["Subject"] = subject
        msg["From"] = f"LetzRyd Ola Automation <{smtp_user}>"
        msg["To"] = recipient

        msg_alt = MIMEMultipart("alternative")
        msg.attach(msg_alt)
        msg_alt.attach(MIMEText(html_body, "html"))

        # Attach inline LetzRyd logo
        if LOGO_PATH.exists():
            with open(LOGO_PATH, "rb") as img_f:
                logo_img = MIMEImage(img_f.read())
                logo_img.add_header("Content-ID", "<letzryd_logo>")
                logo_img.add_header("Content-Disposition", "inline", filename="letzryd_logo.png")
                msg.attach(logo_img)

        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())

        logger(f"[EMAIL] ✉️ Branded email notification successfully sent to {recipient}!")
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
    """Dispatches a branded green success summary email on successful ingestion."""
    subject = f"✅ [SUCCESS] LetzRyd Ola Statement Ingested ({date_range_start} to {date_range_end})"
    title = f"Ola Statement Ingestion Completed"
    subtitle = f"All ride and financial ledger records successfully loaded into PostgreSQL."

    table = f"""
    <table width="100%" cellpadding="6" cellspacing="0" style="font-size: 13px; color: #202124;">
        <tr>
            <td style="font-weight: 600; width: 170px; color: #5f6368;">Target Date Window:</td>
            <td style="font-weight: 700;">{date_range_start} &rarr; {date_range_end}</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Trips Ingested:</td>
            <td style="font-weight: 700; color: #137333;">{trips_count:,} rides</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Financial Ledger Rows:</td>
            <td style="font-weight: 700; color: #137333;">{txns_count:,} rows</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Execution Duration:</td>
            <td>{duration_seconds:.1f} seconds</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Database Status:</td>
            <td><span style="background-color: #ceead6; color: #0d652d; padding: 2px 8px; border-radius: 4px; font-weight: 600;">ACTIVE & COMMITTED</span></td>
        </tr>
    </table>
    """

    cta = ""
    if public_url:
        cta = f"""
        <div style="text-align: center; margin-top: 22px;">
            <a href="{public_url}" style="background-color: #137333; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-size: 14px; font-weight: 700; display: inline-block; box-shadow: 0 2px 6px rgba(19,115,51,0.25);">📥 Download Statement (.xlsx)</a>
        </div>
        """

    html = _get_base_template("SUCCESS", title, subtitle, table, cta)
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
    """Dispatches a branded audit summary email for Tuesday Reconciliation."""
    subject = f"🔍 [AUDIT] LetzRyd Ola Tuesday Reconciliation ({week_start} to {week_end})"
    title = f"Tuesday Reconciliation Audit Report"
    subtitle = f"Automated delta check between Monday baseline and Tuesday statement."

    is_perfect = (trip_diffs == 0 and txn_diffs == 0)
    status_label = "100% RECONCILED (0 Discrepancies)" if is_perfect else f"{trip_diffs} Trip Diffs, {txn_diffs} Ledger Diffs"
    status_badge = "#ceead6; color: #0d652d;" if is_perfect else "#feefc3; color: #b06000;"

    table = f"""
    <table width="100%" cellpadding="6" cellspacing="0" style="font-size: 13px; color: #202124;">
        <tr>
            <td style="font-weight: 600; width: 170px; color: #5f6368;">Audited Week:</td>
            <td style="font-weight: 700;">{week_start} &rarr; {week_end}</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Reconciliation Status:</td>
            <td><span style="background-color: {status_badge}; padding: 2px 8px; border-radius: 4px; font-weight: 700;">{status_label}</span></td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Trip Differences:</td>
            <td style="font-weight: 700;">{trip_diffs} rows in ola_audit_diff_crns</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Ledger Differences:</td>
            <td style="font-weight: 700;">{txn_diffs} rows in ola_audit_diff_transactions</td>
        </tr>
    </table>
    """

    cta = ""
    if public_url:
        cta = f"""
        <div style="text-align: center; margin-top: 22px;">
            <a href="{public_url}" style="background-color: #1a73e8; color: #ffffff; padding: 12px 24px; text-decoration: none; border-radius: 6px; font-size: 14px; font-weight: 700; display: inline-block; box-shadow: 0 2px 6px rgba(26,115,232,0.25);">📊 View Audited Statement (.xlsx)</a>
        </div>
        """

    html = _get_base_template("AUDIT", title, subtitle, table, cta)
    return _send_email(subject, html, recipient_email, logger=logger)

def send_failure_alert(
    date_range_start: str,
    date_range_end: str,
    error_message: str,
    recipient_email: str = DEFAULT_RECIPIENT,
    logger=print
) -> bool:
    """Dispatches a branded red alert email when statement ingestion fails."""
    subject = f"⚠️ [ALERT] LetzRyd Ola Ingestion Failed ({date_range_start} to {date_range_end})"
    title = f"Ola Statement Ingestion Incomplete"
    subtitle = f"The automated pipeline was unable to download the statement from Ola."

    table = f"""
    <table width="100%" cellpadding="6" cellspacing="0" style="font-size: 13px; color: #202124;">
        <tr>
            <td style="font-weight: 600; width: 170px; color: #5f6368;">Target Date Window:</td>
            <td style="font-weight: 700;">{date_range_start} &rarr; {date_range_end}</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Failure Reason:</td>
            <td style="font-weight: 700; color: #c5221f;">{error_message}</td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Database Protection:</td>
            <td><span style="background-color: #ceead6; color: #0d652d; padding: 2px 8px; border-radius: 4px; font-weight: 600;">SAFE & UNTOUCHED</span></td>
        </tr>
        <tr>
            <td style="font-weight: 600; color: #5f6368;">Recovery Action:</td>
            <td>Self-healing cumulative backfill will run automatically on the next scheduled trigger.</td>
        </tr>
    </table>
    """

    html = _get_base_template("FAILURE", title, subtitle, table, "")
    return _send_email(subject, html, recipient_email, logger=logger)

if __name__ == "__main__":
    print("Testing branded email templates...")
    send_success_notification("2026-08-17", "2026-08-23", 6129, 886, "https://storage.googleapis.com/letzryd-ola-raw-statements/statements/2026/08/ola_statement_2026-08-25.xlsx", 28.5)
