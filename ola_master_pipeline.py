"""
ola_master_pipeline.py
======================
Master Orchestrator for Daily Ingestion & Tuesday Audit Reconciliation.

Execution Modes:
----------------
1. Daily Sync Mode (Runs Daily at 10:35 AM):
   - Every day: Downloads YESTERDAY only via the native 'Yesterday' preset (instant direct download, no email needed).
   - Day-by-day accumulation: Each run appends one day's rides & ledger cleanly into PostgreSQL.
   - Downloads statement, uploads to GCS, ingests to ola_raw_crns & ola_raw_transactions, logs to ola_ingestion_log.

2. Tuesday Audit Reconciliation Mode (Runs Tuesday at 08:00 AM):
   - Re-downloads completed prior week (Monday to Sunday) via Custom Date multi-day export.
   - Compares with Monday's statement file.
   - Populates ola_audit_diff_crns & ola_audit_diff_transactions.

Commands:
---------
# Normal automated daily sync (auto-detects yesterday):
python ola_master_pipeline.py

# Force Tuesday Audit run:
python ola_master_pipeline.py --tuesday-audit

# Custom date range sync:
python ola_master_pipeline.py --from-date 2026-08-17 --to-date 2026-08-23
"""

import os
import sys
import time
import argparse
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional, Tuple
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

from hybrid_fetch_ola import download_statement_hybrid
from gcs_upload import upload_statement_to_gcs
from load_ola_to_postgres import load_ola_statement_to_postgres, run_tuesday_audit_reconciliation

DOWNLOAD_DIR = Path(__file__).parent / "ola_downloads"
ARCHIVE_DIR = DOWNLOAD_DIR / "archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

def calculate_daily_date_range(target_d: Optional[date] = None) -> Tuple[date, date, str]:
    """
    Daily Sync Strategy: Always download YESTERDAY only via the native
    'Yesterday' preset on Ola portal (instant direct browser download, no email needed).
    
    Each day's data accumulates in the DB day-by-day:
    - Rides (ola_raw_crns): ON CONFLICT (crn) DO UPDATE — zero duplicates ever.
    - Ledger (ola_raw_transactions): Scoped daily replace (stmt_date) — clean daily accumulation.
    - Tuesday Audit: Still runs on full prior week for 100% financial reconciliation.
    """
    today = target_d or date.today()
    yesterday = today - timedelta(days=1)
    desc = f"Daily Yesterday Sync ({yesterday})"
    return yesterday, yesterday, desc

def calculate_prior_week_dates(target_d: Optional[date] = None) -> Tuple[date, date]:
    today = target_d or date.today()
    weekday = today.weekday()
    # Prior week Monday = today - weekday - 7
    prior_monday = today - timedelta(days=weekday + 7)
    prior_sunday = prior_monday + timedelta(days=6)
    return prior_monday, prior_sunday

def check_if_already_ingested(from_d: date, to_d: date, logger=log) -> bool:
    try:
        from load_ola_to_postgres import get_db_connection
        conn = get_db_connection()
        cur = conn.cursor()
        today = date.today()
        cur.execute("""
            SELECT id, total_crns_imported FROM ola_ingestion_log 
            WHERE date_range_start <= %s AND date_range_end >= %s 
              AND status = 'SUCCESS' 
              AND executed_at::date = %s
            ORDER BY id DESC LIMIT 1;
        """, (from_d, to_d, today))
        row = cur.fetchone()
        conn.close()
        if row:
            logger(f"[Pipeline] [SKIP] Target date range ({from_d} to {to_d}) was ALREADY successfully ingested today (Log ID: #{row[0]}, Trips: {row[1]:,}).")
            logger("[Pipeline] [SKIP] No redundant execution needed. Exiting cleanly with 0 cost!")
            return True
    except Exception as e:
        logger(f"[Pipeline] Warning checking existing ingestion status: {e}")
    return False

def run_daily_sync(
    from_d: date,
    to_d: date,
    run_name: str,
    force_engine: Optional[str] = None,
    logger=log
) -> Optional[dict]:
    logger("\n" + "="*75)
    logger(f"MASTER OLA PIPELINE: {run_name.upper()}")
    logger(f"Target Date Window: {from_d} to {to_d}")
    logger("="*75)
    start_t = time.time()

    # Smart idempotency check: Skip if already ingested today
    if not force_engine and check_if_already_ingested(from_d, to_d, logger=logger):
        return {
            "status": "SUCCESS",
            "skipped": True,
            "message": "Already ingested today."
        }

    try:
        # Step 1: Download Statement File
        downloaded_file = download_statement_hybrid(
            from_date=datetime.combine(from_d, datetime.min.time()),
            to_date=datetime.combine(to_d, datetime.min.time()),
            force_engine=force_engine,
            headless=True,
            logger=logger
        )

        if not downloaded_file or not os.path.exists(downloaded_file):
            err_msg = "Statement file could not be secured from Ola portal or email after all retry attempts."
            logger(f"[Pipeline] [ERROR] {err_msg}")
            try:
                from alerts import send_failure_alert
                send_failure_alert(str(from_d), str(to_d), err_msg, logger=logger)
            except Exception as _ae:
                logger(f"[Pipeline] Warning dispatching alert email: {_ae}")
            return None

        # Step 2: Upload to Google Cloud Storage
        blob_path = f"statements/{from_d.year}/{from_d.strftime('%m')}/{os.path.basename(downloaded_file)}"
        gcs_uri, pub_url = upload_statement_to_gcs(
            local_file_path=downloaded_file,
            custom_blob_name=blob_path,
            logger=logger
        )

        # Step 3: Ingest into PostgreSQL Tables
        duration = round(time.time() - start_t, 2)
        res = load_ola_statement_to_postgres(
            file_path=downloaded_file,
            week_start=from_d,
            week_end=to_d,
            gcs_uri=gcs_uri,
            public_url=pub_url,
            engine_used=force_engine or "hybrid",
            duration_seconds=duration,
            logger=logger
        )

        logger(f"\n[Pipeline] [SUCCESS] Daily Ingestion Completed! (Log ID: {res['log_id']})")
        logger(f"  • Trips Upserted in 'ola_raw_crns':        {res['crns_count']:,}")
        logger(f"  • Transactions in 'ola_raw_transactions':  {res['txns_count']:,}")
        logger(f"  • Public Cloud Storage URL:                 {pub_url}\n")

        # Dispatch Success Summary Email to vendor_aayush@letzryd.com
        try:
            from alerts import send_success_notification
            send_success_notification(
                date_range_start=str(from_d),
                date_range_end=str(to_d),
                trips_count=res['crns_count'],
                txns_count=res['txns_count'],
                public_url=pub_url,
                duration_seconds=duration,
                logger=logger
            )
        except Exception as _ne:
            logger(f"[Pipeline] Warning sending success email: {_ne}")

        return res

    except Exception as e:
        logger(f"[Pipeline] [FATAL ERROR] Pipeline run failed: {e}")
        try:
            from alerts import send_failure_alert
            send_failure_alert(str(from_d), str(to_d), f"Pipeline execution error: {e}", logger=logger)
        except Exception:
            pass
        return None

def run_tuesday_audit(force_engine: Optional[str] = None, logger=log):
    logger("\n" + "="*75)
    logger("TUESDAY RECONCILIATION AUDIT ENGINE (Monday vs Tuesday Delta Check)")
    logger("="*75)

    prior_monday, prior_sunday = calculate_prior_week_dates()
    logger(f"Audit Target Window: {prior_monday} to {prior_sunday}")

    # 1. Check for Monday's Baseline Statement locally or from GCS
    mon_files = list(DOWNLOAD_DIR.glob(f"*{prior_monday.strftime('%Y-%m')}*.xlsx"))
    monday_file = None
    if mon_files:
        monday_file = str(max(mon_files, key=os.path.getmtime))
        logger(f"Found local Monday Baseline Statement: {os.path.basename(monday_file)}")
    else:
        # In Cloud Run (stateless): try fetching Monday baseline from GCS bucket!
        from gcs_upload import download_statement_from_gcs
        today = date.today()
        yesterday_monday = today - timedelta(days=1 if today.weekday() == 1 else today.weekday())
        candidate_blobs = [
            f"statements/{yesterday_monday.year}/{yesterday_monday.strftime('%m')}/ola_statement_{yesterday_monday.strftime('%Y-%m-%d')}.xlsx",
            f"statements/{prior_monday.year}/{prior_monday.strftime('%m')}/ola_statement_{prior_monday.strftime('%Y-%m-%d')}.xlsx",
            f"statements/{prior_sunday.year}/{prior_sunday.strftime('%m')}/ola_statement_{prior_sunday.strftime('%Y-%m-%d')}.xlsx",
        ]
        for gcs_blob in candidate_blobs:
            local_dest = DOWNLOAD_DIR / os.path.basename(gcs_blob)
            if download_statement_from_gcs(gcs_blob, str(local_dest), logger=logger):
                monday_file = str(local_dest)
                logger(f"Retrieved Monday Baseline from GCS: {os.path.basename(monday_file)}")
                break

    if not monday_file:
        logger("[Audit] No Monday baseline file found in local or GCS. Ingesting today's statement as baseline.")
        return run_daily_sync(prior_monday, prior_sunday, "Tuesday Audit Baseline", force_engine, logger)

    # 2. Download Fresh Tuesday Audit File
    tue_file = download_statement_hybrid(
        from_date=datetime.combine(prior_monday, datetime.min.time()),
        to_date=datetime.combine(prior_sunday, datetime.min.time()),
        force_engine=force_engine,
        headless=True,
        logger=logger
    )

    if not tue_file or not os.path.exists(tue_file):
        logger("[Audit] [ERROR] Could not download Tuesday statement.")
        return None

    # 3. Upload Tuesday Statement to GCS
    blob_path = f"statements/{prior_monday.year}/{prior_monday.strftime('%m')}/{os.path.basename(tue_file)}"
    gcs_uri, pub_url = upload_statement_to_gcs(
        local_file_path=tue_file,
        custom_blob_name=blob_path,
        logger=logger
    )

    # 4. Run Audit Reconciliation Engine
    audit_res = run_tuesday_audit_reconciliation(
        monday_file_path=monday_file,
        tuesday_file_path=tue_file,
        week_start=prior_monday,
        week_end=prior_sunday,
        logger=logger
    )

    logger(f"\n[Audit] [SUCCESS] Tuesday Audit Reconciliation Finished!")
    logger(f"  • Trip Discrepancies Found:        {audit_res['trip_diffs_count']} rows in 'ola_audit_diff_crns'")
    logger(f"  • Transaction Discrepancies Found: {audit_res['txn_diffs_count']} rows in 'ola_audit_diff_transactions'")

    # Dispatch Tuesday Audit Summary Email to vendor_aayush@letzryd.com
    try:
        from alerts import send_audit_notification
        send_audit_notification(
            week_start=str(prior_monday),
            week_end=str(prior_sunday),
            trip_diffs=audit_res['trip_diffs_count'],
            txn_diffs=audit_res['txn_diffs_count'],
            public_url=pub_url,
            logger=logger
        )
    except Exception as _ae:
        logger(f"[Audit] Warning sending audit email: {_ae}")

    return audit_res

def main():
    parser = argparse.ArgumentParser(description="LetzRyd Ola Master Ingestion & Audit Pipeline")
    parser.add_argument("--tuesday-audit", action="store_true", help="Run Tuesday 8:00 AM audit reconciliation")
    parser.add_argument("--from-date", help="Custom from date YYYY-MM-DD")
    parser.add_argument("--to-date", help="Custom to date YYYY-MM-DD")
    parser.add_argument("--force-engine", choices=["playwright", "browser_use"], default=None)

    # Defensive parsing: strip stray wrapper tokens if passed via container overrides
    filtered_argv = [arg for arg in sys.argv[1:] if not (arg.endswith(".py") or arg == "python")]
    args, unknown = parser.parse_known_args(filtered_argv)

    is_tuesday_audit = args.tuesday_audit or os.environ.get("TUESDAY_AUDIT", "false").lower() == "true"

    res = None
    if is_tuesday_audit:
        res = run_tuesday_audit(force_engine=args.force_engine)
    elif args.from_date and args.to_date:
        f_d = datetime.strptime(args.from_date, "%Y-%m-%d").date()
        t_d = datetime.strptime(args.to_date, "%Y-%m-%d").date()
        res = run_daily_sync(f_d, t_d, f"Custom Date Sync ({f_d} to {t_d})", force_engine=args.force_engine)
    else:
        w_start, w_end, run_desc = calculate_daily_date_range()
        res = run_daily_sync(w_start, w_end, run_desc, force_engine=args.force_engine)

    if not res or res.get("status") != "SUCCESS":
        log("[Pipeline] [FATAL] Execution completed without data ingestion. Exiting with failure status.")
        sys.exit(1)

if __name__ == "__main__":
    main()
