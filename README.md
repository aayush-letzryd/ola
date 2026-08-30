# 🚖 LetzRyd Ola Automation & Tuesday Reconciliation Engine

> **Production-grade automated daily statement ingestion, Cloud Storage raw archiving, and Tuesday reconciliation audit engine for LetzRyd's Ola fleet (700+ vehicles).**

---

## 📑 Table of Contents
1. [Architecture & Workflow](#-architecture--workflow)
2. [Database Schema (5 PostgreSQL Tables)](#-database-schema-5-postgresql-tables)
3. [Weekly Operations Timetable](#-weekly-operations-timetable)
4. [Dual-Engine Execution & Fallback](#-dual-engine-execution--fallback)
5. [Email Polling & 60-Minute Cooldown Logic](#-email-polling--60-minute-cooldown-logic)
6. [Google Cloud Deployment & Scheduling Guide](#-google-cloud-deployment--scheduling-guide)
7. [Running Locally](#-running-locally)

---

## 🏛 Architecture & Workflow

The system is designed with **dual-engine redundancy**, **zero-duplicate database ingestion**, and **automated dispute reconciliation**:

```
                  ┌────────────────────────────────────────┐
                  │       DAILY SCHEDULE: 10:35 AM         │
                  └──────────────────┬─────────────────────┘
                                     │
                        ┌────────────▼────────────┐
                        │   ENGINE 1: PLAYWRIGHT  │
                        │    (Fast Automation)    │
                        └────────────┬────────────┘
                                     │
                         ┌───────────┴───────────┐
                         ▼                       ▼
                    [SUCCESS]                 [FAILURE]
                        │                        │
                        │               ┌────────▼────────┐
                        │               │    ENGINE 2:    │
                        │               │ AI BROWSER-USE  │
                        │               │ (Vision Backup) │
                        │               └────────┬────────┘
                        │                        │
                        └────────────┬───────────┘
                                     │
                        ┌────────────▼────────────┐
                        │    DOWNLOAD STATEMENT   │
                        │   (Direct or IMAP Poll) │
                        └────────────┬────────────┘
                                     │
                        ┌────────────▼────────────┐
                        │   CLOUD STORAGE UPLOAD  │
                        │   (Public Backup Link)  │
                        └────────────┬────────────┘
                                     │
                        ┌────────────▼────────────┐
                        │   POSTGRESQL INGESTION  │
                        │  (Trips, Ledger & Logs) │
                        └─────────────────────────┘
```

---

## 🗄 Database Schema (5 PostgreSQL Tables)

| # | Table Name | Purpose | Ingestion Strategy |
| :- | :--- | :--- | :--- |
| **1** | **`ola_ingestion_log`** | Master execution history with public Cloud Storage download links. | Append `INSERT` per run. |
| **2** | **`ola_raw_crns`** | Master trip table (all 26 raw columns from `RawCrns`). | **Atomic UPSERT (`ON CONFLICT (crn) DO UPDATE`)** |
| **3** | **`ola_raw_transactions`** | Master financial ledger (all 9 raw columns from `RawTransactions`). | **Scoped Daily Replace (`DELETE WHERE date_for BETWEEN %s AND %s` $\rightarrow$ `Batch INSERT`)** |
| **4** | **`ola_audit_diff_crns`** | Tuesday Trip Differences vs Monday (tolls, fares, cash). | Diff `INSERT` with `status = 'PENDING'`. |
| **5** | **`ola_audit_diff_transactions`** | Tuesday Ledger Differences vs Monday (incentives, fees). | **Bucket Matching Algorithm** with `status = 'PENDING'`. |

---

## ⏰ Weekly Operations Timetable

* **Daily Rolling Week Ingestion (Hourly 6:00 AM – 11:00 AM IST):**
  * **Every Day:** Ingests the **`Rolling Week`** (from Monday of current week through Yesterday).
  * **Auto-Healing & Completeness:** Automatically backfills any missed days, updates post-midnight incentives, and reconciles late-settled fees.
  * **Zero Duplicates:** Rides are upserted atomically (`ON CONFLICT DO UPDATE`), and ledger entries are replaced cleanly (`WHERE date_for BETWEEN from_d AND to_d`).
  * **Smart Idempotency:** If the 6:00 AM run succeeds, all subsequent hourly runs (7, 8, 9, 10, 11 AM) check PostgreSQL and exit in **<1 second for ₹0 cost**.
* **Tuesday Audit Reconciliation (Tuesdays @ 08:00 AM IST):**
  * Re-downloads completed prior week (`Monday to Sunday` 7 days).
  * Compares against Monday's baseline file to catch late toll adjustments, disputes, and bonus credits.
  * Dispatches the executive audit email with detailed trip/transaction diff counts.

---

## ☁️ Google Cloud Deployment & Scheduling Guide

### Serverless Cloud Run Job + Cloud Scheduler
1. **Deploy to Cloud Run Job (`ola-sync-job`)**:
   ```bash
   ./deploy.sh
   ```
2. **Cloud Scheduler Cron Schedules**:
   * **Daily Rolling Week Sync & Hourly Retries**:
     `0 6,7,8,9,10,11 * * *` (Runs every hour on the hour from 6:00 AM to 11:00 AM IST)
   * **Tuesday 08:00 AM Audit Reconciliation**:
     `0 8 * * 2` (Runs every Tuesday at 08:00 AM IST)

---

## 💻 Running Locally

```powershell
# 1. Install Dependencies
pip install -r requirements.txt
playwright install chromium

# 2. Run Daily Sync Manually
python ola_master_pipeline.py

# 3. Run Tuesday Audit Manually
python ola_master_pipeline.py --tuesday-audit

# 4. Start Continuous Background Scheduler Daemon
python scheduler_daemon.py
```
