"""
scheduler_daemon.py
===================
Continuous Background Scheduler Daemon for LetzRyd Ola Automation.

Schedules:
----------
1. Daily 10:35 AM (Mon-Sun): Runs ola_master_pipeline (Daily Ingestion)
2. Tuesdays 08:00 AM:        Runs ola_master_pipeline --tuesday-audit (Audit Delta Check)

Usage:
------
python scheduler_daemon.py
"""

import os
import sys
import time
import subprocess
from datetime import datetime, date
from pathlib import Path

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

PYTHON_EXE = sys.executable
PIPELINE_SCRIPT = Path(r"C:\Users\anura\RYD\letzryd-ola-integration\ola_master_pipeline.py")

DAILY_RUN_TIME = os.environ.get("DAILY_RUN_TIME", "10:35")
TUESDAY_AUDIT_TIME = os.environ.get("TUESDAY_AUDIT_TIME", "08:00")

def run_job(args_list, job_name):
    log(f"\n[Scheduler] >>> TRIGGERING JOB: {job_name} <<<")
    cmd = [PYTHON_EXE, str(PIPELINE_SCRIPT)] + args_list
    try:
        proc = subprocess.run(cmd, capture_output=False, text=True)
        log(f"[Scheduler] Job '{job_name}' completed with exit code: {proc.returncode}")
    except Exception as e:
        log(f"[Scheduler] [ERROR] Job '{job_name}' encountered exception: {e}")

def main():
    log("="*75)
    log("LETZRYD OLA AUTOMATED SCHEDULER SERVICE")
    log(f"  • Daily Ingestion Schedule:    Everyday at {DAILY_RUN_TIME} AM")
    log(f"  • Tuesday Audit Schedule:      Tuesdays at {TUESDAY_AUDIT_TIME} AM")
    log(f"  • Pipeline Script:             {PIPELINE_SCRIPT}")
    log("="*75)

    last_daily_run_date = None
    last_audit_run_date = None

    while True:
        now = datetime.now()
        curr_date = now.date()
        curr_time_str = now.strftime("%H:%M")
        weekday = now.weekday() # 0 = Mon, 1 = Tue, ..., 6 = Sun

        # 1. Check Tuesday Audit (08:00 AM on Tuesdays)
        if weekday == 1 and curr_time_str == TUESDAY_AUDIT_TIME and last_audit_run_date != curr_date:
            run_job(["--tuesday-audit"], "Tuesday Reconciliation Audit")
            last_audit_run_date = curr_date

        # 2. Check Daily Ingestion (10:35 AM Everyday)
        if curr_time_str == DAILY_RUN_TIME and last_daily_run_date != curr_date:
            run_job([], "Daily Ola Ingestion Sync")
            last_daily_run_date = curr_date

        time.sleep(20)

if __name__ == "__main__":
    main()
