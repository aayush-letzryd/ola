# 🚗 LetzRyd Ola Automation — Master Knowledge Transfer (KT) & Operations Runbook

**Version:** `2.0 Production Ready`  
**Last Updated:** `25 August 2026`  
**Author & Fleet Owner:** `Aayush (vendor_aayush@letzryd.com)`  
**Google Cloud Project:** `letzryd-dev-test` (Region: `asia-south1` / Mumbai)  
**Production Database:** PostgreSQL 15 (`35.200.196.113:5432 / postgres`)  
**GitHub Repository:** [https://github.com/aayush-letzryd/ola](https://github.com/aayush-letzryd/ola)

---

## 📌 1. System Overview & Executive Purpose

LetzRyd Mobility Private Limited operates an electric commercial vehicle (EV) fleet on the **Ola Cabs Operator Platform**. Every day, Ola generates extensive multi-sheet financial accounting workbooks containing ride-level fares, driver deductions, cash collected, FASTag tolls, TDS deductions, and weekly performance incentives.

This automation system is a **100% serverless, zero-maintenance data pipeline** engineered to:
1. **Automate Daily Ingestion:** Seamlessly download cumulative week-to-date and weekly settlement statements.
2. **Ensure 100% Mathematical Precision:** Upsert rides into `ola_raw_crns` and financial ledgers into `ola_raw_transactions` with zero duplicates.
3. **Execute Tuesday Reconciliation Audits:** Perform deep 360° delta comparisons between Monday baseline statements and final Tuesday statements.
4. **Deliver Executive Status Emails:** Dispatch branded HTML status cards with your official LetzRyd logo to `vendor_aayush@letzryd.com` on every run.
5. **Run at ₹0.00 Serverless Cost:** Operates entirely within the Google Cloud Free Tier using Cloud Run Jobs and Cloud Scheduler.

---

## 🏗️ 2. High-Level System Architecture

```
                                  ┌────────────────────────────────────────────────┐
                                  │           Google Cloud Scheduler               │
                                  │  • 10:35 AM IST: Daily Sync Trigger            │
                                  │  • 11:35 AM IST: Daily Retry Trigger           │
                                  │  • 08:00 AM IST: Tuesday Audit (Tuesdays)      │
                                  └───────────────────────┬────────────────────────┘
                                                          │ (HTTP POST Invocation)
                                                          ▼
                                  ┌────────────────────────────────────────────────┐
                                  │       Google Cloud Run Jobs (Serverless)       │
                                  │           Container: 'ola-sync-job'            │
                                  │           Image: asia-south1-docker            │
                                  └───────────────────────┬────────────────────────┘
                                                          │
                   ┌──────────────────────────────────────┴──────────────────────────────────────┐
                   ▼                                                                             ▼
    ┌──────────────────────────────┐                                              ┌──────────────────────────────┐
    │       Authentication         │                                              │      Dual-Engine Scraper     │
    │  • SMSOLA Google Sheet Relay │                                              │  • Engine 1: Playwright      │
    │  • Baseline OTP Polling      │                                              │  • Engine 2: Gemini Flash AI │
    │  • Auto-relogin & Session    │                                              │  • 40m IMAP Gmail Polling    │
    └──────────────┬───────────────┘                                              └──────────────┬───────────────┘
                   │                                                                             │
                   └──────────────────────────────────────┬──────────────────────────────────────┘
                                                          ▼
                                  ┌────────────────────────────────────────────────┐
                                  │           Data Processing & Upload             │
                                  │  • GCS: gs://letzryd-ola-raw-statements/       │
                                  │  • PostgreSQL: 5 Tables Ingestion & Diffs      │
                                  │  • Alerts: Branded HTML Email Dispatch         │
                                  └────────────────────────────────────────────────┘
```

---

## 📅 3. Day-of-Week Calculation & Scraper Matrix

| Day of Week | Date Range Requested | Scraper Selection Method | Business Purpose |
| :--- | :--- | :--- | :--- |
| **Monday** | Prior Mon $\rightarrow$ Sun (7 Days) | **`Custom Date` (Exact)** | Finalizes prior week to lock driver payouts |
| **Tuesday (Daily)** | Monday (Single Day = Yesterday) | **`Yesterday` Preset** | Starts tracking new ongoing week (Direct Download) |
| **Tuesday (Audit)** | Prior Mon $\rightarrow$ Sun (7 Days) | **`Custom Date` (Exact)** | Audits Monday baseline vs final Tuesday settlement |
| **Wednesday** | Current Mon $\rightarrow$ Tue (2 Days) | **`Custom Date` (Exact)** | Cumulative 2-day week-to-date sync |
| **Thursday** | Current Mon $\rightarrow$ Wed (3 Days) | **`Custom Date` (Exact)** | Cumulative 3-day week-to-date sync |
| **Friday** | Current Mon $\rightarrow$ Thu (4 Days) | **`Custom Date` (Exact)** | Cumulative 4-day week-to-date sync |
| **Saturday / Sunday** | Current Mon $\rightarrow$ Yesterday | **`Custom Date` (Exact)** | Cumulative week-to-date sync through weekend |

### 🛡️ The Self-Healing Cumulative Guarantee:
Because Wed–Sun runs are strictly cumulative from Monday, if an outage occurs on any single day, **the very next run automatically requests the full cumulative date range and backfills all missed data without manual intervention!**

---

## 🗄️ 4. Production Database Schema (PostgreSQL)

All tables reside in PostgreSQL instance `35.200.196.113:5432 / postgres`:

### 1. `ola_raw_crns` (Rides & Trips Master Table)
* **Write Strategy:** Atomic `ON CONFLICT (crn) DO UPDATE` (29 columns).
* **Synthetic Fallback:** Generates `SYNTH_{veh}_{date}_{idx}` if Ola CRN is null on cancellation rows.
* **Columns:** `crn (PK), stmt_date, vehicle_number, driver_name, driver_number, completion_status, customer_bill_raw, paid_by_ola_money_raw, operator_bill_raw, peak_pricing_raw, ride_earnings_raw, tds_raw, toll_parking_raw, cash_collected_by_driver_raw, ola_to_pay, category, pickup_time, actual_kms_raw, trip_time_raw, fare_raw, share_osns, number_of_share_osns, bookings_completed_raw, ride_type, pickup_location, drop_location, week_start, week_end, source_file, created_at`.

### 2. `ola_raw_transactions` (Financial Ledger Table)
* **Write Strategy:** Scoped Week Replace (`DELETE WHERE week_start = %s AND week_end = %s` $\rightarrow$ `Batch INSERT`).
* **Columns:** `id (PK), stmt_date, transaction_type, vehicle_number, car_model, date_for, amount_raw, transaction_status, sub_category, payment_type, week_start, week_end, source_file, created_at`.

### 3. `ola_ingestion_log` (Master Audit Trail)
* **Columns:** `id (PK), executed_at, date_range_start, date_range_end, run_name, engine_used, source_filename, gcs_uri, public_url, file_size_bytes, total_crns_imported, total_transactions_imported, status, duration_seconds, error_message, created_at`.

### 4. `ola_audit_diff_crns` (Tuesday Trip Discrepancies)
* **Columns:** `id (PK), crn, vehicle_number, stmt_date, week_start, week_end, diff_customer_bill, diff_operator_bill, diff_cash_collected, diff_toll_amount, diff_tds_amount, diff_peak_pricing, diff_ride_earnings, diff_ola_to_pay, diff_actual_kms, monday_status, tuesday_status, status_change, monday_operator_bill, tuesday_operator_bill, monday_cash_collected, tuesday_cash_collected, monday_toll_amount, tuesday_toll_amount, change_type, audit_status, created_at`.

### 5. `ola_audit_diff_transactions` (Tuesday Ledger Discrepancies)
* **Columns:** `id (PK), vehicle_number, week_start, week_end, date_for, transaction_type, sub_category, payment_type, monday_count, tuesday_count, diff_count, monday_sum, tuesday_sum, diff_amount, change_type, status, created_at`.

---

## 🔍 5. Tuesday Reconciliation Audit Engine

* **Stateless Cloud Run Baseline Download:** Automatically retrieves Monday's raw statement from GCS (`gs://letzryd-ola-raw-statements/statements/YYYY/MM/ola_statement_YYYY-MM-DD.xlsx`).
* **360° Trip Delta Comparison:** Matches every ride on CRN and computes exact financial deltas down to `₹0.01` sensitivity.
* **Multiset Bucket Matching:** Groups ledger entries by `(vehicle_number | date_for | transaction_type | sub_category | payment_type)` with `dropna=False` to detect modified incentive bonuses or IMPS deductions regardless of row sequence.

---

## ✉️ 6. Executive Branded Email Notifications

Dispatches styled HTML email status reports to **`vendor_aayush@letzryd.com`** on **every single run**:
* 🚗 **LetzRyd EV Logo:** Embedded via inline MIME Content-ID (`cid:letzryd_logo`) for immediate rendering without clicking "Load images".
* 🟢 **Success Email:** Shows target date window, total rides ingested, ledger rows, duration, and direct GCS download CTA button.
* 🔍 **Tuesday Audit Email:** Displays reconciliation comparison status, trip diffs count (`0`), and ledger diffs count (`0`).
* 🔴 **Failure Alert Email:** Dispatches immediate alert with exact failure diagnostics while confirming database safety.

---

## 🚀 7. Operations Guide & Direct Console Links

### 🔗 Direct Google Cloud Console Links:
* **Cloud Scheduler Dashboard (Force Run):**  
  👉 [https://console.cloud.google.com/cloudscheduler?project=letzryd-dev-test](https://console.cloud.google.com/cloudscheduler?project=letzryd-dev-test)
* **Cloud Run Jobs Dashboard (Execution Status & Logs):**  
  👉 [https://console.cloud.google.com/run/jobs?project=letzryd-dev-test](https://console.cloud.google.com/run/jobs?project=letzryd-dev-test)
* **Google Cloud Storage Bucket (Raw Statement Files):**  
  👉 [https://console.cloud.google.com/storage/browser/letzryd-ola-raw-statements](https://console.cloud.google.com/storage/browser/letzryd-ola-raw-statements)
* **SMS OTP Relay Google Sheet:**  
  👉 [https://docs.google.com/spreadsheets/d/1KrJ022-HfOBNnRVky7DBebCGm6jGcfk3OV3UqcHagIA/edit](https://docs.google.com/spreadsheets/d/1KrJ022-HfOBNnRVky7DBebCGm6jGcfk3OV3UqcHagIA/edit)

---

## 💻 8. Quick Commands Reference (Google Cloud Shell)

### 1. Rebuild & Deploy Container Image:
```bash
cd ~/ola && git pull && gcloud builds submit --tag asia-south1-docker.pkg.dev/letzryd-dev-test/cloud-run-source-deploy/ola-sync-job:latest
```

### 2. Force Run Daily Sync:
```bash
gcloud scheduler jobs run ola-daily-sync-trigger --location=asia-south1
```

### 3. Force Run Tuesday Audit:
```bash
gcloud scheduler jobs run ola-tuesday-audit-trigger --location=asia-south1
```

### 4. Cancel a Running Task:
```bash
gcloud run jobs executions cancel <EXECUTION_NAME> --region=asia-south1
```

---

## 📊 9. DBeaver SQL Verification Cheat Sheet

```sql
-- 1. View Latest Ingestion History & URLs
SELECT id, executed_at, date_range_start, date_range_end, engine_used, 
       total_crns_imported, total_transactions_imported, status, duration_seconds, public_url 
FROM ola_ingestion_log 
ORDER BY id DESC LIMIT 10;

-- 2. Inspect Ingested Rides
SELECT crn, stmt_date, vehicle_number, driver_name, completion_status,
       customer_bill_raw, operator_bill_raw, ride_earnings_raw, ola_to_pay 
FROM ola_raw_crns 
ORDER BY stmt_date DESC LIMIT 10;

-- 3. Inspect Financial Ledger
SELECT id, stmt_date, vehicle_number, transaction_type, sub_category, payment_type, amount_raw 
FROM ola_raw_transactions 
ORDER BY id DESC LIMIT 10;

-- 4. Inspect Tuesday Audit Discrepancies
SELECT * FROM ola_audit_diff_crns ORDER BY id DESC LIMIT 10;
SELECT * FROM ola_audit_diff_transactions ORDER BY id DESC LIMIT 10;
```

---

## 🛠️ 10. Troubleshooting SOP (Standard Operating Procedures)

| Issue | Cause | Standard Operating Procedure |
| :--- | :--- | :--- |
| **Ola Statement Not Sent to Email** | Peak portal traffic | Attempt 2 automatically fires 2 modal bursts at 11:35 AM. Next morning's cumulative sync automatically captures it. |
| **OTP Not Arriving in Sheet** | Macro delay on SMS device | Verify the Android SMS relay device has internet access and Google Sheet permissions. |
| **Container Skipped in 1 Second** | Smart Idempotency check | Indicates today's data is already in PostgreSQL. If you want to force re-ingestion, run with custom `--from-date` and `--to-date`. |
| **Tuesday Audit Ran Daily Sync** | Missing request body override | Verify `ola-tuesday-audit-trigger` has `--message-body='{"overrides":{"containerOverrides":[{"args":["--tuesday-audit"]}]}}'`. |

---
**LetzRyd Mobility Private Limited • Fleet Operations Certified**
