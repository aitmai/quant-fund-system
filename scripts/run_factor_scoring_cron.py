"""
Factor scoring cron entrypoint — DESIGN.md §3 Stage 2, §7 ("Weekly cron").

Usage:
    python scripts/run_factor_scoring_cron.py                     # cron
    python scripts/run_factor_scoring_cron.py --manual --triggered-by aitmai
    python scripts/run_factor_scoring_cron.py --score-date 2026-07-01
"""

import argparse
import sys
from datetime import date

sys.path.insert(0, ".")

# Must run BEFORE any `src.*` import below — see run_ingestion_cron.py
# for why placement matters (module-level env reads in provider files).
from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.scoring import run_factor_scoring


def main() -> int:
    parser = argparse.ArgumentParser(description="Run weekly factor scoring (momentum, low-vol, quality, value).")
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--triggered-by", default=None)
    parser.add_argument("--score-date", default=None, help="YYYY-MM-DD; defaults to today (mainly for backtest/manual re-runs)")
    args = parser.parse_args()

    run_type = "manual" if args.manual else "cron"
    score_date = date.fromisoformat(args.score_date) if args.score_date else None
    conn = get_connection()

    try:
        with JobRun(conn, stage="factor_scoring", run_type=run_type, triggered_by=args.triggered_by) as job:
            summary = run_factor_scoring.run(conn, job=job, score_date=score_date)
            print(f"Factor scoring summary: {summary}")
        return 0
    except Exception as exc:
        print(f"ERROR: factor scoring job failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
