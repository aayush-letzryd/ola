"""
hybrid_fetch_ola.py
===================
Master Hybrid Statement Downloader for LetzRyd Ola Pipeline.

Implements the Dual-Engine Architecture:
- Primary Engine (Option 1): Custom Playwright (fetch_ola_statement.py)
  * Fast (~15-25s), $0.00 cost, direct DOM selector execution.
- Fallback Engine (Option 2): AI Vision Agent (fetch_ola_browser_use.py)
  * Multimodal Gemini Flash agent with visual element grounding.
  * Automatically activates if Option 1 encounters timeouts, UI layout changes, or DOM exceptions.
"""

import os
import sys
import time
import asyncio
from datetime import datetime, date
from typing import Optional, Dict, Any

from fetch_ola_statement import fetch_ola_statement
from fetch_ola_browser_use import (
    execute_browser_download_run,
    calculate_date_range_for_day,
    verify_and_archive_statement,
    DOWNLOAD_DIR
)

def download_statement_hybrid(
    from_date: datetime,
    to_date: datetime,
    force_engine: Optional[str] = None,
    headless: bool = False,
    logger=print
) -> Optional[str]:
    """
    Executes statement download using the Hybrid Pattern:
    1. Tries Option 1 (Deterministic Playwright).
    2. If Playwright fails or encounters UI drift, catches the exception and
       automatically triggers Option 2 (Browser-Use AI Vision Agent).
    """
    from_str = from_date.strftime("%Y-%m-%d")
    to_str = to_date.strftime("%Y-%m-%d")
    from_human = from_date.strftime("%d %B %Y")
    to_human = to_date.strftime("%d %B %Y")

    logger("\n" + "="*75)
    logger(f"HYBRID STATEMENT DOWNLOADER")
    logger(f"Date Range: {from_str} ({from_human}) to {to_str} ({to_human})")
    logger(f"Execution Strategy: Primary = Playwright, Fallback = Browser-Use AI")
    logger("="*75 + "\n")

    # -----------------------------------------------------------------------
    # ENGINE 1: Deterministic Playwright (Primary Fast-Path)
    # -----------------------------------------------------------------------
    if force_engine != "browser_use":
        logger("[Hybrid Downloader] 🚀 Attempting Engine 1: Custom Playwright (Fast Path)...")
        start_time = time.time()
        try:
            downloaded_file = fetch_ola_statement(
                from_date=from_date,
                to_date=to_date,
                logger=logger
            )
            elapsed = time.time() - start_time
            if downloaded_file and os.path.exists(downloaded_file):
                logger(f"\n[Hybrid Downloader] [✓] Engine 1 (Playwright) SUCCESS in {elapsed:.1f}s!")
                logger(f"Saved to: {downloaded_file}")
                return downloaded_file

        except Exception as err:
            elapsed = time.time() - start_time
            logger(f"\n[Hybrid Downloader] [!] Engine 1 (Playwright) failed after {elapsed:.1f}s: {err}")
            if str(err).startswith("DEFERRED:"):
                # Handled deferred email submission
                logger(f"[Hybrid Downloader] Email modal was submitted by Playwright. Handing over to IMAP fallback...")

    # -----------------------------------------------------------------------
    # ENGINE 2: Browser-Use AI Vision Agent (Autonomous Fallback)
    # -----------------------------------------------------------------------
    if force_engine != "playwright":
        logger("\n" + "-"*75)
        logger("[Hybrid Downloader] ⚠️ ACTIVATING ENGINE 2: Browser-Use AI Agent (Fallback)...")
        logger("Reason: Engine 1 failed or force_engine=browser_use.")
        logger("-"*75 + "\n")

        run_info = {
            "run_name": f"Hybrid AI Fallback ({from_str} to {to_str})",
            "from_date": from_date,
            "to_date": to_date,
            "from_str": from_str,
            "to_str": to_str,
            "from_human": from_human,
            "to_human": to_human,
            "purpose": "Autonomous AI fallback download via Gemini Vision"
        }

        start_time = time.time()
        try:
            downloaded_file = asyncio.run(
                execute_browser_download_run(
                    run_info=run_info,
                    headless=headless,
                    logger=logger
                )
            )
            elapsed = time.time() - start_time
            if downloaded_file and os.path.exists(downloaded_file):
                logger(f"\n[Hybrid Downloader] [✓] Engine 2 (Browser-Use AI) SUCCESS in {elapsed:.1f}s!")
                logger(f"Saved to: {downloaded_file}")
                return downloaded_file
            else:
                logger(f"\n[Hybrid Downloader] [!] Engine 2 completed but no file was returned.")

        except Exception as err:
            elapsed = time.time() - start_time
            logger(f"\n[Hybrid Downloader] [!] Engine 2 (Browser-Use) encountered error after {elapsed:.1f}s: {err}")

    logger("\n[Hybrid Downloader] [!] CRITICAL: Both Primary and Fallback engines failed.")
    return None
