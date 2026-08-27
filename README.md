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

* **Daily Ingestion (Mon–Sun @ 10:35 AM):**
  * **Every Day:** Downloads **`Yesterday`** only via the native preset for instant direct browser `.xlsx` download (~20 seconds, zero email dependency).
  * Rides are upserted with zero duplicates (`ON CONFLICT`).
  * Ledger entries accumulate day-by-day with scoped daily refresh (`WHERE date_for = yesterday`).
* **Tuesday Audit Reconciliation (Tuesdays @ 08:00 AM):**
  * Re-downloads completed prior week (`Monday to Sunday` 7 days).
  * Compares against Monday's baseline file to catch late toll adjustments, disputes, and bonus credits.
  * Dispatches the executive audit email with detailed trip/transaction diff counts.

---

## ☁️ Google Cloud Deployment & Scheduling Guide

### Option 1: Cloud Scheduler + Compute Engine VM (Recommended for GUI / Chrome)
1. **Create an e2-medium Compute Engine VM** in GCP (`asia-south1` Mumbai).
2. **Install Python & Chrome**:
   ```bash
   sudo apt update && sudo apt install -y python3-pip git chromium-browser
   git clone git@github.com:aayush-letzryd/letzryd-ola-integration.git
   cd letzryd-ola-integration
   pip install -r requirements.txt
   playwright install chromium
   ```
3. **Configure Cron on VM**:
   ```bash
   crontab -e
   ```
   Add the following lines:
   ```cron
   # Daily 10:35 AM Ingestion (Mon-Sun)
   35 10 * * * cd /home/user/letzryd-ola-integration && python3 ola_master_pipeline.py >> /var/log/ola_daily.log 2>&1

   # Tuesday 08:00 AM Audit Reconciliation
   0 8 * * 2 cd /home/user/letzryd-ola-integration && python3 ola_master_pipeline.py --tuesday-audit >> /var/log/ola_audit.log 2>&1
   ```

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
