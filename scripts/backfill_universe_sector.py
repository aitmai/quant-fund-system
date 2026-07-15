"""
One-off backfill: populate sector (and company_name/cik) for tickers
already sitting in `universe` from before src/universe/construct_universe.py's
sector-scraping logic existed — DESIGN.md §3 Stage 1, §6.1.

Why this is a SEPARATE script rather than just telling you to re-run
scripts/run_universe_sync.py: sync_index_universe() has now been fixed
to upsert every fetched ticker (not just newly-added ones), so a normal
sync run WOULD also fix this going forward. But sync also does
liquidity filtering and add/remove diffing every time it runs, and this
backfill is really a one-time data-repair job for the 501 rows that
predate the fix — keeping it separate means you get a focused before/
after report of exactly what this repair changed, without mixing that
signal into a routine sync's regular +N/-N output.

Safe to run more than once — same COALESCE-based upsert that
sync_index_universe() itself uses, so it only fills in what's
currently NULL and never overwrites a correct existing value.

Usage:
    python scripts/backfill_universe_sector.py
    python scripts/backfill_universe_sector.py --include-russell1000
"""

import argparse
import sys
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.universe import index_sources


def _fetch_sources(include_russell1000: bool):
    combined = {}
    for row in index_sources.fetch_sp500_constituents():
        combined[row["ticker"]] = row
    if include_russell1000:
        for row in index_sources.fetch_russell1000_constituents():
            combined.setdefault(row["ticker"], row)  # don't clobber S&P 500 data for overlapping tickers
    return combined


def main():
    parser = argparse.ArgumentParser(description="Backfill missing universe.sector for pre-existing tickers.")
    parser.add_argument("--include-russell1000", action="store_true")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total, COUNT(sector) AS with_sector FROM universe WHERE is_active = TRUE")
            before_total, before_with_sector = cur.fetchone()

        fetched = _fetch_sources(args.include_russell1000)
        if not fetched:
            print(
                "ERROR: fetched zero tickers from index_sources — check the WARNING lines above "
                "(Wikipedia fetch failure, layout change, etc). Nothing to backfill.",
                file=sys.stderr,
            )
            sys.exit(1)

        today = date.today()
        with JobRun(conn, stage="backfill_universe_sector", run_type="manual", triggered_by=args.triggered_by) as job:
            updated = 0
            with conn:
                with conn.cursor() as cur:
                    for ticker, info in fetched.items():
                        cur.execute(
                            """
                            INSERT INTO universe (ticker, company_name, sector, exchange, is_active, source, added_date, cik)
                            VALUES (%s, %s, %s, NULL, TRUE, 'auto', %s, %s)
                            ON CONFLICT (ticker) DO UPDATE SET
                                company_name = COALESCE(EXCLUDED.company_name, universe.company_name),
                                sector = COALESCE(EXCLUDED.sector, universe.sector),
                                cik = COALESCE(EXCLUDED.cik, universe.cik)
                            """,
                            (ticker, info.get("company_name"), info.get("sector"), today, info.get("cik")),
                        )
                        updated += 1

            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS total, COUNT(sector) AS with_sector FROM universe WHERE is_active = TRUE")
                after_total, after_with_sector = cur.fetchone()

            job.note(
                f"processed {updated} fetched tickers; with_sector {before_with_sector} -> {after_with_sector} "
                f"(of {after_total} active)"
            )
            print(f"Processed {updated} fetched tickers.")
            print(f"Active universe sector coverage: {before_with_sector}/{before_total} -> {after_with_sector}/{after_total}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
