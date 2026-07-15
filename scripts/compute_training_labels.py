"""
Training-label computation entrypoint — DESIGN.md §13 Phase 3 prep.

Usage:
    python scripts/compute_training_labels.py
    python scripts/compute_training_labels.py --horizon 21
"""

import argparse
import sys

sys.path.insert(0, ".")

# Must run BEFORE any `src.*` import below — see run_ingestion_cron.py
# for why placement matters (module-level env reads in provider files).
from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.scoring import compute_labels


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute training_labels from factor_scores + realized forward returns.")
    parser.add_argument("--horizon", type=int, default=None,
                         help="Forward-return horizon in trading days (default: LABEL_HORIZON_TRADING_DAYS env var, or 21)")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()

    conn = get_connection()
    try:
        with JobRun(conn, stage="compute_training_labels", run_type="manual",
                    triggered_by=args.triggered_by) as job:
            summary = compute_labels.compute_labels(conn, horizon=args.horizon)
            job.note(
                f"candidates={summary['candidates']} labeled={summary['labeled']} "
                f"not_yet_computable={summary['not_yet_computable']}"
            )
        print(f"Training labels summary: {summary}")
        return 0
    except Exception as exc:
        print(f"ERROR: training label computation failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
