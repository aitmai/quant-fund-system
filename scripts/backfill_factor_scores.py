"""
Retroactive factor-score backfill — Phase 3 (Stage 3 ML Ranking) prep.

DESIGN.md §13 Phase 3 flags this as the faster alternative to waiting 3
real-time years for factor_scores history to accumulate: Stage 2's
z-scores can be computed retroactively over price/fundamentals history
already ingested, using the SAME point-in-time-correct code path
(as_of-parameterized) real weekly cron runs already use — see
src/scoring/data_fetch.py's as_of handling and DESIGN.md §8.2. No new
scoring logic here; this just drives run_factor_scoring.run() across
many historical dates instead of just today.

KNOWN LIMITATION (accepted for v1, decided 2026-07-14): scores against
TODAY's active universe applied retroactively, not actual historical S&P
500 membership at each past date. universe_changes only tracks
membership from whenever this project started syncing (not 3 years
back), so true point-in-time universe reconstruction isn't possible with
data currently in the DB. This is real survivorship bias in the v1
training set — revisit if/when a historical constituents data source is
added.

Every backfilled run is tagged triggered_by='backfill' in job_runs
(distinct from real cron/manual runs) — both for auditability and so
this script can resume cleanly after an interruption without redoing
completed dates or creating duplicate rows under a new run_id.

Usage:
    python scripts/backfill_factor_scores.py                     # full 3-year backfill, weekly (Mondays)
    python scripts/backfill_factor_scores.py --years 1            # shorter backfill, e.g. for testing
    python scripts/backfill_factor_scores.py --start 2024-01-01 --end 2024-06-01
"""

import argparse
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

# Must run BEFORE any `src.*` import below — see run_ingestion_cron.py
# for why placement matters (module-level env reads in provider files).
from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.scoring import run_factor_scoring

BACKFILL_TRIGGERED_BY = "backfill"


def weekly_mondays(start: date, end: date):
    """Every Monday from start to end inclusive — matches the weekly
    cadence real Stage 2 cron runs use, so backfilled history has the
    same date spacing live data will eventually have. If start itself
    isn't a Monday, begins from the first Monday on/after it."""
    days_until_monday = (7 - start.weekday()) % 7
    current = start if start.weekday() == 0 else start + timedelta(days=days_until_monday)
    dates = []
    while current <= end:
        dates.append(current)
        current += timedelta(days=7)
    return dates


def already_backfilled_dates(conn) -> set:
    """Dates a PRIOR backfill run already completed — resumability, so
    re-running this script after an interruption doesn't redo finished
    work or create duplicate (ticker, score_date) rows under a new
    run_id (factor_scores' primary key includes run_id, so nothing would
    error, but it would silently waste computation and leave multiple
    backfill-origin rows for the same date, defeating the point of
    resumability)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT fs.score_date
            FROM factor_scores fs
            JOIN job_runs jr ON jr.run_id = fs.run_id
            WHERE jr.triggered_by = %s AND jr.status = 'complete'
            """,
            (BACKFILL_TRIGGERED_BY,),
        )
        return {row[0] for row in cur.fetchall()}


def run_backfill(conn, dates_to_run) -> dict:
    """Runs run_factor_scoring.run() for each date, one JobRun per date
    (mirrors how real cron logs each run individually). A failure on one
    date is logged and skipped rather than aborting the whole backfill —
    same "don't let one bad date stall everything" philosophy as the
    ingestion crons."""
    processed, failed = 0, 0
    for i, score_date in enumerate(dates_to_run, start=1):
        print(f"[{i}/{len(dates_to_run)}] Backfilling {score_date}...")
        try:
            with JobRun(conn, stage="factor_scoring", run_type="manual",
                        triggered_by=BACKFILL_TRIGGERED_BY) as job:
                summary = run_factor_scoring.run(conn, job=job, score_date=score_date)
            print(f"  -> {summary['scored_at_least_one']}/{summary['total_active']} scored")
            processed += 1
        except Exception as exc:
            print(f"  ERROR backfilling {score_date}: {exc}", file=sys.stderr)
            failed += 1
            continue
    return {"processed": processed, "failed": failed}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Retroactively backfill factor_scores over already-ingested "
                    "price/fundamentals history (Phase 3 prep)."
    )
    parser.add_argument("--years", type=float, default=3.0,
                         help="How many years back from --end (default 3, matches min_training_history_years)")
    parser.add_argument("--start", default=None, help="YYYY-MM-DD; overrides --years if given")
    parser.add_argument("--end", default=None, help="YYYY-MM-DD; defaults to today")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=int(args.years * 365))

    conn = get_connection()
    try:
        all_dates = weekly_mondays(start, end)
        already_done = already_backfilled_dates(conn)
        remaining = [d for d in all_dates if d not in already_done]

        print(
            f"Backfill plan: {len(all_dates)} weekly dates from {start} to {end} — "
            f"{len(already_done)} already done, {len(remaining)} remaining."
        )
        if not remaining:
            print("Nothing to do — all dates already backfilled.")
            return 0

        summary = run_backfill(conn, remaining)
        print(f"Backfill run complete: {summary['processed']} processed, {summary['failed']} failed.")
        return 1 if summary["failed"] and not summary["processed"] else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
