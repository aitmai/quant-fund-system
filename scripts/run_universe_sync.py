"""
Universe sync entrypoint — DESIGN.md §7 Cron Job Summary ("Universe
reconciliation | Monthly + event-driven").

Usage:
    python scripts/run_universe_sync.py                          # S&P 500 only, cron
    python scripts/run_universe_sync.py --include-russell1000
    python scripts/run_universe_sync.py --manual --triggered-by aitmai
    python scripts/run_universe_sync.py --apply-liquidity-filter --min-avg-dollar-volume 500000
"""

import argparse
import sys

sys.path.insert(0, ".")  # allow `python scripts/run_universe_sync.py` from repo root

from src.db import get_connection
from src.job_run import JobRun
from src.universe.construct_universe import (
    apply_liquidity_filters,
    sync_index_universe,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync universe against index constituent lists.")
    parser.add_argument("--include-russell1000", action="store_true", help="Also fetch Russell 1000 (best-effort, see index_sources.py)")
    parser.add_argument("--no-sp500", action="store_true", help="Skip S&P 500 fetch (rare — mostly for testing Russell-only)")
    parser.add_argument("--manual", action="store_true", help="Tag this run as manual rather than cron")
    parser.add_argument("--triggered-by", default=None)
    parser.add_argument("--apply-liquidity-filter", action="store_true", help="Also recompute avg_dollar_volume and deactivate illiquid tickers")
    parser.add_argument("--min-avg-dollar-volume", type=float, default=1_000_000)
    args = parser.parse_args()

    run_type = "manual" if args.manual else "cron"
    conn = get_connection()

    try:
        with JobRun(conn, stage="universe_sync", run_type=run_type, triggered_by=args.triggered_by) as job:
            result = sync_index_universe(
                conn,
                include_sp500=not args.no_sp500,
                include_russell1000=args.include_russell1000,
                run_type=run_type,
                triggered_by=args.triggered_by,
            )
            print(f"Universe sync: +{result['added']} / -{result['removed']} (fetched {result['total_fetched']} tickers)")
            job.note(f"added={result['added']} removed={result['removed']} fetched={result['total_fetched']}")

            if args.apply_liquidity_filter:
                liq = apply_liquidity_filters(conn, min_avg_dollar_volume=args.min_avg_dollar_volume)
                print(f"Liquidity filter: evaluated {liq['evaluated']}, deactivated {liq['deactivated']}")
                job.note(f"liquidity_evaluated={liq['evaluated']} liquidity_deactivated={liq['deactivated']}")
        return 0
    except Exception as exc:
        print(f"ERROR: universe sync failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
