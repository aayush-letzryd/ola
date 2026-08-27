"""
load_ola_to_postgres.py
=======================
Production PostgreSQL Loader & Tuesday Audit Reconciliation Engine.

Loads:
1. ola_raw_crns (Using ON CONFLICT (crn) DO UPDATE + synthetic fallback for null CRNs)
2. ola_raw_transactions (Using Scoped Week Replace: DELETE WHERE week_start AND week_end -> Batch INSERT)
3. ola_ingestion_log (Master execution history with public GCS download URLs)
4. ola_audit_diff_crns (Tuesday Trip Differences vs Monday)
5. ola_audit_diff_transactions (Tuesday Financial Ledger Differences vs Monday using Bucket Matching)
"""

import os
import re
import time
import json
import traceback
from datetime import datetime, date
from pathlib import Path
from decimal import Decimal
from typing import Optional, Tuple, Dict, Any, List
import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)

def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "35.200.196.113"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "postgres"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASS"),
    )

def _coerce_numeric(val) -> float:
    if pd.isna(val) or val is None or val == "":
        return 0.0
    s = str(val).strip().replace(",", "").replace("₹", "").replace(" ", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0

def _coerce_date(val) -> Optional[date]:
    if pd.isna(val) or val is None or val == "":
        return None
    if isinstance(val, (date, datetime)):
        return val.date() if isinstance(val, datetime) else val
    try:
        dt = pd.to_datetime(val, errors="coerce")
        if pd.notna(dt):
            return dt.date()
    except Exception:
        pass
    return None

def _normalize_vehicle(val) -> str:
    if pd.isna(val) or val is None:
        return ""
    return re.sub(r"\s+", "", str(val).strip().upper())

# ---------------------------------------------------------------------------
# Core Ingestion Engine
# ---------------------------------------------------------------------------
def load_ola_statement_to_postgres(
    file_path: str,
    week_start: date,
    week_end: date,
    gcs_uri: Optional[str] = None,
    public_url: Optional[str] = None,
    engine_used: str = "playwright",
    duration_seconds: float = 0.0,
    logger=print
) -> Dict[str, Any]:
    start_time = time.time()
    filename = os.path.basename(file_path)
    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0

    logger(f"\n[DB Loader] Starting ingestion for file: {filename} ({file_size:,} bytes)")
    logger(f"[DB Loader] Target Window: {week_start} to {week_end}")

    conn = get_db_connection()
    conn.autocommit = False
    cur = conn.cursor()

    try:
        xl = pd.ExcelFile(file_path)

        # -------------------------------------------------------------------
        # 1. Ingest RawCrns
        # -------------------------------------------------------------------
        crn_sheet = [s for s in xl.sheet_names if "crn" in s.lower()][0]
        df_crns = pd.read_excel(file_path, sheet_name=crn_sheet)
        logger(f"[DB Loader] Read {len(df_crns)} rows from '{crn_sheet}' sheet.")

        crn_records = []
        for idx, row in df_crns.iterrows():
            raw_crn = str(row.get("CRN", "")).strip() if pd.notna(row.get("CRN")) else ""
            veh = _normalize_vehicle(row.get("Car number"))
            stmt_d = _coerce_date(row.get("Date"))
            p_time = str(row.get("Pick up time", "")).strip() if pd.notna(row.get("Pick up time")) else ""

            if not raw_crn:
                if not veh and pd.isna(row.get("Customer Bill Raw")):
                    continue # Skip empty footer
                raw_crn = f"SYNTH_{veh}_{stmt_d}_{idx}"

            crn_records.append((
                raw_crn,
                stmt_d,
                veh,
                str(row.get("Driver name", "")).strip() if pd.notna(row.get("Driver name")) else "",
                str(row.get("Driver number", "")).strip() if pd.notna(row.get("Driver number")) else "",
                str(row.get("Completion Status", "")).strip() if pd.notna(row.get("Completion Status")) else "",
                _coerce_numeric(row.get("Customer Bill Raw")),
                _coerce_numeric(row.get("Paid by ola money Raw")),
                _coerce_numeric(row.get("Operator Bill Raw")),
                _coerce_numeric(row.get("Peak Pricing Raw")),
                _coerce_numeric(row.get("Ride earnings Raw")),
                _coerce_numeric(row.get("TDS Raw")),
                _coerce_numeric(row.get("Toll/ Parking Raw")),
                _coerce_numeric(row.get("Cash collected by driver Raw")),
                _coerce_numeric(row.get("Ola to Pay")),
                str(row.get("Category", "")).strip() if pd.notna(row.get("Category")) else "",
                p_time,
                _coerce_numeric(row.get("Actual Kms Raw")),
                str(row.get("Trip Time Raw", "")).strip() if pd.notna(row.get("Trip Time Raw")) else "",
                _coerce_numeric(row.get("Fare Raw")),
                str(row.get("Share OSNs", "")).strip() if pd.notna(row.get("Share OSNs")) else "",
                int(_coerce_numeric(row.get("Number of share OSNs"))),
                int(_coerce_numeric(row.get("Bookings Completed Raw"))) or 1,
                str(row.get("Ride Type", "")).strip() if pd.notna(row.get("Ride Type")) else "",
                str(row.get("PickUp Location", "")).strip() if pd.notna(row.get("PickUp Location")) else "",
                str(row.get("Drop Location", "")).strip() if pd.notna(row.get("Drop Location")) else "",
                week_start,
                week_end,
                filename
            ))

        upsert_crns_sql = """
            INSERT INTO ola_raw_crns (
                crn, stmt_date, vehicle_number, driver_name, driver_number, completion_status,
                customer_bill_raw, paid_by_ola_money_raw, operator_bill_raw, peak_pricing_raw,
                ride_earnings_raw, tds_raw, toll_parking_raw, cash_collected_by_driver_raw,
                ola_to_pay, category, pickup_time, actual_kms_raw, trip_time_raw, fare_raw,
                share_osns, number_of_share_osns, bookings_completed_raw, ride_type,
                pickup_location, drop_location, week_start, week_end, source_file
            ) VALUES %s
            ON CONFLICT (crn) DO UPDATE SET
                stmt_date = EXCLUDED.stmt_date,
                vehicle_number = EXCLUDED.vehicle_number,
                driver_name = EXCLUDED.driver_name,
                driver_number = EXCLUDED.driver_number,
                completion_status = EXCLUDED.completion_status,
                customer_bill_raw = EXCLUDED.customer_bill_raw,
                paid_by_ola_money_raw = EXCLUDED.paid_by_ola_money_raw,
                operator_bill_raw = EXCLUDED.operator_bill_raw,
                peak_pricing_raw = EXCLUDED.peak_pricing_raw,
                ride_earnings_raw = EXCLUDED.ride_earnings_raw,
                tds_raw = EXCLUDED.tds_raw,
                toll_parking_raw = EXCLUDED.toll_parking_raw,
                cash_collected_by_driver_raw = EXCLUDED.cash_collected_by_driver_raw,
                ola_to_pay = EXCLUDED.ola_to_pay,
                category = EXCLUDED.category,
                pickup_time = EXCLUDED.pickup_time,
                actual_kms_raw = EXCLUDED.actual_kms_raw,
                trip_time_raw = EXCLUDED.trip_time_raw,
                fare_raw = EXCLUDED.fare_raw,
                share_osns = EXCLUDED.share_osns,
                number_of_share_osns = EXCLUDED.number_of_share_osns,
                bookings_completed_raw = EXCLUDED.bookings_completed_raw,
                ride_type = EXCLUDED.ride_type,
                pickup_location = EXCLUDED.pickup_location,
                drop_location = EXCLUDED.drop_location,
                week_start = EXCLUDED.week_start,
                week_end = EXCLUDED.week_end,
                source_file = EXCLUDED.source_file;
        """
        psycopg2.extras.execute_values(cur, upsert_crns_sql, crn_records, page_size=2000)
        logger(f"[DB Loader] [SUCCESS] Upserted {len(crn_records)} trips into 'ola_raw_crns'.")

        # -------------------------------------------------------------------
        # 2. Ingest RawTransactions (Daily Replace — Yesterday-Only Mode)
        # -------------------------------------------------------------------
        txn_sheet = [s for s in xl.sheet_names if "trans" in s.lower() or "acc" in s.lower()][0]
        df_txns = pd.read_excel(file_path, sheet_name=txn_sheet)
        logger(f"[DB Loader] Read {len(df_txns)} rows from '{txn_sheet}' sheet.")

        # Scoped Daily Delete: Only wipe rows for this specific date (stmt_date).
        # This preserves ALL previous days' ledger rows so they accumulate cleanly.
        cur.execute(
            "DELETE FROM ola_raw_transactions WHERE stmt_date = %s;",
            (week_end,)  # week_end == yesterday in Yesterday-only daily mode
        )
        logger(f"[DB Loader] Cleaned previous staging rows for stmt_date={week_end} in 'ola_raw_transactions'.")

        txn_records = []
        for idx, row in df_txns.iterrows():
            veh = _normalize_vehicle(row.get("Car number"))
            if not veh and pd.isna(row.get("Amount Raw")):
                continue
            txn_records.append((
                _coerce_date(row.get("Date")),
                str(row.get("Type", "")).strip() if pd.notna(row.get("Type")) else "",
                veh,
                str(row.get("Car model", "")).strip() if pd.notna(row.get("Car model")) else "",
                _coerce_date(row.get("Date for")),
                _coerce_numeric(row.get("Amount Raw")),
                str(row.get("Status", "")).strip() if pd.notna(row.get("Status")) else "",
                str(row.get("Sub Category", "")).strip() if pd.notna(row.get("Sub Category")) else "",
                str(row.get("Payment type", "")).strip().lower() if pd.notna(row.get("Payment type")) else "",
                week_start,
                week_end,
                filename
            ))

        insert_txns_sql = """
            INSERT INTO ola_raw_transactions (
                stmt_date, transaction_type, vehicle_number, car_model, date_for,
                amount_raw, transaction_status, sub_category, payment_type,
                week_start, week_end, source_file
            ) VALUES %s;
        """
        psycopg2.extras.execute_values(cur, insert_txns_sql, txn_records, page_size=2000)
        logger(f"[DB Loader] [SUCCESS] Inserted {len(txn_records)} rows into 'ola_raw_transactions'.")

        # -------------------------------------------------------------------
        # 3. Log Ingestion to ola_ingestion_log
        # -------------------------------------------------------------------
        elapsed = round(time.time() - start_time + duration_seconds, 2)
        cur.execute("""
            INSERT INTO ola_ingestion_log (
                date_range_start, date_range_end, run_name, engine_used,
                source_filename, gcs_uri, public_url, file_size_bytes,
                total_crns_imported, total_transactions_imported,
                status, duration_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            week_start,
            week_end,
            f"Ola Sync ({week_start} to {week_end})",
            engine_used,
            filename,
            gcs_uri,
            public_url,
            file_size,
            len(crn_records),
            len(txn_records),
            "SUCCESS",
            elapsed
        ))
        log_id = cur.fetchone()[0]
        conn.commit()

        logger(f"[DB Loader] [SUCCESS] Ingestion successfully committed! (Log ID: {log_id})")
        return {
            "status": "SUCCESS",
            "log_id": log_id,
            "crns_count": len(crn_records),
            "txns_count": len(txn_records),
            "public_url": public_url,
            "elapsed_s": elapsed
        }

    except Exception as e:
        conn.rollback()
        logger(f"[DB Loader] [!] Ingestion failed: {e}")
        traceback.print_exc()
        try:
            cur.execute("""
                INSERT INTO ola_ingestion_log (
                    date_range_start, date_range_end, source_filename,
                    status, error_message
                ) VALUES (%s, %s, %s, %s, %s);
            """, (week_start, week_end, filename, "FAILED", str(e)))
            conn.commit()
        except Exception:
            pass
        raise e
    finally:
        cur.close()
        conn.close()

# ---------------------------------------------------------------------------
# Tuesday Audit Reconciliation Engine (Monday vs Tuesday Diff Generator)
# ---------------------------------------------------------------------------
def run_tuesday_audit_reconciliation(
    monday_file_path: str,
    tuesday_file_path: str,
    week_start: date,
    week_end: date,
    logger=print
) -> Dict[str, Any]:
    """
    Executes deep reconciliation between Monday's baseline file and Tuesday's audit file:
    1. Compares trips by CRN -> writes diffs to ola_audit_diff_crns
    2. Compares transactions using Bucket Matching -> writes diffs to ola_audit_diff_transactions
    """
    logger(f"\n[Audit Engine] Starting Tuesday Reconciliation for Week ({week_start} to {week_end})")
    logger(f"  Monday Baseline:  {os.path.basename(monday_file_path)}")
    logger(f"  Tuesday Audit:    {os.path.basename(tuesday_file_path)}")

    # 1. Reconcile Trips (RawCrns - 360 Degree Comparison)
    df_mon_crns = pd.read_excel(monday_file_path, sheet_name="RawCrns")
    df_tue_crns = pd.read_excel(tuesday_file_path, sheet_name="RawCrns")

    df_mon_crns["crn_clean"] = df_mon_crns["CRN"].astype(str).str.strip()
    df_tue_crns["crn_clean"] = df_tue_crns["CRN"].astype(str).str.strip()

    merged_crns = pd.merge(
        df_mon_crns,
        df_tue_crns,
        on="crn_clean",
        how="outer",
        suffixes=("_mon", "_tue")
    )

    crn_diffs = []
    for _, r in merged_crns.iterrows():
        crn = str(r["crn_clean"])
        veh = _normalize_vehicle(r.get("Car number_tue") or r.get("Car number_mon"))
        stmt_d = _coerce_date(r.get("Date_tue") or r.get("Date_mon"))
        
        # Numeric Diffs (Tuesday - Monday)
        mon_cust = _coerce_numeric(r.get("Customer Bill Raw_mon"))
        tue_cust = _coerce_numeric(r.get("Customer Bill Raw_tue"))
        diff_cust = round(tue_cust - mon_cust, 2)

        mon_bill = _coerce_numeric(r.get("Operator Bill Raw_mon"))
        tue_bill = _coerce_numeric(r.get("Operator Bill Raw_tue"))
        diff_bill = round(tue_bill - mon_bill, 2)

        mon_cash = _coerce_numeric(r.get("Cash collected by driver Raw_mon"))
        tue_cash = _coerce_numeric(r.get("Cash collected by driver Raw_tue"))
        diff_cash = round(tue_cash - mon_cash, 2)

        mon_toll = _coerce_numeric(r.get("Toll/ Parking Raw_mon"))
        tue_toll = _coerce_numeric(r.get("Toll/ Parking Raw_tue"))
        diff_toll = round(tue_toll - mon_toll, 2)

        mon_tds = _coerce_numeric(r.get("TDS Raw_mon"))
        tue_tds = _coerce_numeric(r.get("TDS Raw_tue"))
        diff_tds = round(tue_tds - mon_tds, 2)

        mon_peak = _coerce_numeric(r.get("Peak Pricing Raw_mon"))
        tue_peak = _coerce_numeric(r.get("Peak Pricing Raw_tue"))
        diff_peak = round(tue_peak - mon_peak, 2)

        mon_earn = _coerce_numeric(r.get("Ride earnings Raw_mon"))
        tue_earn = _coerce_numeric(r.get("Ride earnings Raw_tue"))
        diff_earn = round(tue_earn - mon_earn, 2)

        mon_ola_pay = _coerce_numeric(r.get("Ola to Pay_mon"))
        tue_ola_pay = _coerce_numeric(r.get("Ola to Pay_tue"))
        diff_ola_pay = round(tue_ola_pay - mon_ola_pay, 2)

        mon_kms = _coerce_numeric(r.get("Actual Kms Raw_mon"))
        tue_kms = _coerce_numeric(r.get("Actual Kms Raw_tue"))
        diff_kms = round(tue_kms - mon_kms, 2)

        # Status Tracking
        mon_stat = str(r.get("Completion Status_mon") or "").strip()
        tue_stat = str(r.get("Completion Status_tue") or "").strip()
        stat_chg = f"{mon_stat} -> {tue_stat}" if mon_stat != tue_stat and mon_stat and tue_stat else ""

        has_num_diff = any(d != 0 for d in [diff_cust, diff_bill, diff_cash, diff_toll, diff_tds, diff_peak, diff_earn, diff_ola_pay, diff_kms])

        if pd.isna(r.get("CRN_mon")):
            change_t = "NEW_RIDE_ADDED"
        elif pd.isna(r.get("CRN_tue")):
            change_t = "RIDE_REMOVED"
        elif stat_chg:
            change_t = "STATUS_CHANGED"
        elif has_num_diff:
            change_t = "FARE_OR_TOLL_ADJUSTED"
        else:
            continue # Exactly matched

        crn_diffs.append((
            crn, veh, stmt_d, week_start, week_end,
            diff_cust, diff_bill, diff_cash, diff_toll, diff_tds,
            diff_peak, diff_earn, diff_ola_pay, diff_kms,
            mon_stat, tue_stat, stat_chg,
            mon_bill, tue_bill, mon_cash, tue_cash, mon_toll, tue_toll,
            change_t, "PENDING"
        ))

    # 2. Reconcile Transactions (RawTransactions - Bucket Matching)
    df_mon_txns = pd.read_excel(monday_file_path, sheet_name="RawTransactions")
    df_tue_txns = pd.read_excel(tuesday_file_path, sheet_name="RawTransactions")

    def _prepare_buckets(df):
        records = []
        for _, row in df.iterrows():
            veh = _normalize_vehicle(row.get("Car number"))
            d_for = _coerce_date(row.get("Date for"))
            t_type = str(row.get("Type", "")).strip()
            sub_cat = str(row.get("Sub Category", "")).strip()
            p_type = str(row.get("Payment type", "")).strip().lower()
            amt = _coerce_numeric(row.get("Amount Raw"))
            if veh:
                records.append({
                    "key": f"{veh}|{d_for}|{t_type}|{sub_cat}|{p_type}",
                    "vehicle_number": veh,
                    "date_for": d_for,
                    "transaction_type": t_type,
                    "sub_category": sub_cat,
                    "payment_type": p_type,
                    "amount": amt
                })
        df_clean = pd.DataFrame(records)
        if df_clean.empty:
            return pd.DataFrame(columns=["key", "vehicle_number", "date_for", "transaction_type", "sub_category", "payment_type", "count", "sum_amount"])
        grouped = df_clean.groupby(
            ["key", "vehicle_number", "date_for", "transaction_type", "sub_category", "payment_type"],
            dropna=False
        ).agg(
            count=("amount", "count"),
            sum_amount=("amount", "sum")
        ).reset_index()
        return grouped

    mon_buckets = _prepare_buckets(df_mon_txns)
    tue_buckets = _prepare_buckets(df_tue_txns)

    merged_txns = pd.merge(
        mon_buckets,
        tue_buckets,
        on=["key", "vehicle_number", "date_for", "transaction_type", "sub_category", "payment_type"],
        how="outer",
        suffixes=("_mon", "_tue")
    )

    txn_diffs = []
    for _, r in merged_txns.iterrows():
        veh = str(r["vehicle_number"])
        d_for = r["date_for"]
        t_type = str(r["transaction_type"])
        sub_cat = str(r["sub_category"])
        p_type = str(r["payment_type"])

        mon_cnt = 0 if pd.isna(r.get("count_mon")) else int(r.get("count_mon"))
        tue_cnt = 0 if pd.isna(r.get("count_tue")) else int(r.get("count_tue"))
        diff_cnt = tue_cnt - mon_cnt

        mon_sum = round(_coerce_numeric(r.get("sum_amount_mon")), 2)
        tue_sum = round(_coerce_numeric(r.get("sum_amount_tue")), 2)
        diff_amt = round(tue_sum - mon_sum, 2)

        if diff_amt == 0 and diff_cnt == 0:
            continue # Exactly matched

        if mon_cnt == 0 and tue_cnt > 0:
            chg = "NEW_INCENTIVE_OR_ENTRY_ADDED" if "incentive" in sub_cat.lower() else "NEW_TRANSACTION_ADDED"
        elif mon_cnt > 0 and tue_cnt == 0:
            chg = "TRANSACTION_REMOVED"
        else:
            chg = "AMOUNT_OR_FEE_MODIFIED"

        txn_diffs.append((
            veh, week_start, week_end, d_for,
            t_type, sub_cat, p_type,
            mon_cnt, tue_cnt, diff_cnt,
            mon_sum, tue_sum, diff_amt,
            chg, "PENDING"
        ))

    # 3. Save Diffs into PostgreSQL
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Clear existing audit diffs for target week before inserting fresh audit results
        cur.execute("DELETE FROM ola_audit_diff_crns WHERE week_start = %s AND week_end = %s;", (week_start, week_end))
        cur.execute("DELETE FROM ola_audit_diff_transactions WHERE week_start = %s AND week_end = %s;", (week_start, week_end))

        if crn_diffs:
            sql_crn = """
                INSERT INTO ola_audit_diff_crns (
                    crn, vehicle_number, stmt_date, week_start, week_end,
                    diff_customer_bill, diff_operator_bill, diff_cash_collected, diff_toll_amount, diff_tds_amount,
                    diff_peak_pricing, diff_ride_earnings, diff_ola_to_pay, diff_actual_kms,
                    monday_status, tuesday_status, status_change,
                    monday_operator_bill, tuesday_operator_bill, monday_cash_collected, tuesday_cash_collected, monday_toll_amount, tuesday_toll_amount,
                    change_type, audit_status
                ) VALUES %s;
            """
            psycopg2.extras.execute_values(cur, sql_crn, crn_diffs)

        if txn_diffs:
            sql_txn = """
                INSERT INTO ola_audit_diff_transactions (
                    vehicle_number, week_start, week_end, date_for,
                    transaction_type, sub_category, payment_type,
                    monday_count, tuesday_count, diff_count,
                    monday_sum, tuesday_sum, diff_amount,
                    change_type, status
                ) VALUES %s;
            """
            psycopg2.extras.execute_values(cur, sql_txn, txn_diffs)

        conn.commit()
        logger(f"[Audit Engine] [SUCCESS] Reconciliation complete:")
        logger(f"  - Trip Diffs Found:        {len(crn_diffs)} rows written to 'ola_audit_diff_crns'")
        logger(f"  - Transaction Diffs Found: {len(txn_diffs)} rows written to 'ola_audit_diff_transactions'")

        return {
            "status": "SUCCESS",
            "trip_diffs_count": len(crn_diffs),
            "txn_diffs_count": len(txn_diffs)
        }
    except Exception as e:
        conn.rollback()
        logger(f"[Audit Engine] [ERROR] DB write failed — rolled back safely: {e}")
        raise
    finally:
        cur.close()
        conn.close()

if __name__ == "__main__":
    test_file = r"C:\Users\anura\RYD\letzryd-ola-integration\ola_downloads\ola_statement_2026-08-24.xlsx"
    w_start = date(2026, 8, 17)
    w_end = date(2026, 8, 23)
    pub_url = "https://storage.googleapis.com/letzryd-ola-raw-statements/statements/2026/08/ola_statement_2026-08-24.xlsx"
    g_uri = "gs://letzryd-ola-raw-statements/statements/2026/08/ola_statement_2026-08-24.xlsx"

    load_ola_statement_to_postgres(
        file_path=test_file,
        week_start=w_start,
        week_end=w_end,
        gcs_uri=g_uri,
        public_url=pub_url,
        engine_used="playwright",
        logger=print
    )
