"""
Daily ingestion cron entrypoint — DESIGN.md §5, §7.

Runs price and fundamentals ingestion back to back, each against its own
independent daily budget (so a slow fundamentals day never blocks price
ingestion or vice versa), each logged as its own job_runs row.

Usage:
    python scripts/run_ingestion_cron.py                  # both, cron
    python scripts/run_ingestion_cron.py --only price      # backfill/refresh price_history only
    python scripts/run_ingestion_cron.py --only fundamentals
    python scripts/run_ingestion_cron.py --manual --triggered-by aitmai
"""

import argparse
import sys

sys.path.insert(0, ".")

from src.db import get_connection
from src.ingestion import fundamentals_ingestion, price_ingestion
from src.job_run import JobRun


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daily price/fundamentals ingestion cron.")
    parser.add_argument("--only", choices=["price", "fundamentals"], default=None)
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()

    run_type = "manual" if args.manual else "cron"
    conn = get_connection()
    exit_code = 0

    try:
        if args.only in (None, "price"):
            try:
                with JobRun(conn, stage="ingestion_price", run_type=run_type, triggered_by=args.triggered_by) as job:
                    summary = price_ingestion.run(conn, job=job)
                    print(f"Price ingestion: {summary}")
            except Exception as exc:
                print(f"ERROR: price ingestion job failed: {exc}", file=sys.stderr)
                exit_code = 1

        if args.only in (None, "fundamentals"):
            try:
                with JobRun(conn, stage="ingestion_fundamentals", run_type=run_type, triggered_by=args.triggered_by) as job:
                    summary = fundamentals_ingestion.run(conn, job=job)
                    print(f"Fundamentals ingestion: {summary}")
            except Exception as exc:
                print(f"ERROR: fundamentals ingestion job failed: {exc}", file=sys.stderr)
                exit_code = 1

        return exit_code
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
