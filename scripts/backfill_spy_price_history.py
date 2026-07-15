"""
SPY price history backfill — Phase 9 (Hedge Sleeve), DESIGN.md §3.

The GARCH(1,1) forward-vol forecast needs SPY's own price history. SPY
is an ETF, not an S&P 500 constituent, so Phase 1's ingestion never
touches it — the active-universe queries in price_ingestion.py,
fundamentals_ingestion.py, and data_fetch.py (factor scoring) all filter
on `universe.is_active = TRUE`.

Deliberately does NOT use scripts/upload_manual_tickers.py /
upload_manual_tickers() — that helper unconditionally sets
is_active = TRUE, which would sweep SPY into ALL THREE of those active-
universe loops: price ingestion (harmless but wasteful), fundamentals
ingestion (SPY doesn't file 10-Ks the way a stock does — would likely
just fail/waste budget), and worst of all Stage 2 factor scoring, which
would sector-z-score SPY as if it were a long-sleeve candidate stock.
SPY is a hedge/benchmark instrument, not a trading candidate.

Instead: inserts the `universe` row directly with is_active = FALSE
(satisfies price_history's FK to universe.ticker, but excludes SPY from
every is_active-gated query), then fetches price history via
YFinanceProvider directly — bypassing the shared cron ingestion loop
entirely, since this is a one-time/occasional backfill for a single
fixed ticker, not part of the recurring active-universe workflow.

Usage:
    python scripts/backfill_spy_price_history.py                # 3-year default backfill
    python scripts/backfill_spy_price_history.py --years 5
"""

import argparse
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.providers.yfinance_provider import YFinanceProvider

SPY_TICKER = "SPY"


def ensure_spy_in_universe(conn):
    """Inserts SPY into `universe` with is_active = FALSE if not already
    present — satisfies price_history's FK without pulling SPY into any
    active-universe-gated ingestion/scoring loop. Idempotent: does
    nothing if SPY is already there (does NOT flip an existing row's
    is_active, in case it was deliberately set some other way)."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO universe (ticker, company_name, sector, exchange, is_active, source, added_date)
                VALUES (%s, %s, %s, %s, FALSE, 'manual', %s)
                ON CONFLICT (ticker) DO NOTHING
                """,
                (SPY_TICKER, "SPDR S&P 500 ETF Trust", "ETF - Benchmark/Hedge Instrument", "NYSEARCA", date.today()),
            )


def backfill_spy_prices(conn, years: float) -> int:
    provider = YFinanceProvider()
    start_date = date.today() - timedelta(days=int(years * 365))
    bars = provider.fetch_history(SPY_TICKER, start_date=start_date)

    if not bars:
        return 0

    with conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO price_history (ticker, date, open, high, low, close, volume, adj_close)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, date) DO UPDATE SET
                    open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                    close = EXCLUDED.close, volume = EXCLUDED.volume, adj_close = EXCLUDED.adj_close
                """,
                [(b.ticker, b.date, b.open, b.high, b.low, b.close, b.volume, b.adj_close) for b in bars],
            )
    return len(bars)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill SPY price history for the hedge sleeve's GARCH input.")
    parser.add_argument("--years", type=float, default=3.0, help="Years of history to backfill (default 3)")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()

    conn = get_connection()
    try:
        ensure_spy_in_universe(conn)
        # Distinct stage name from 'ingestion_price' — this is NOT part
        # of the shared active-universe cron loop, and using the same
        # stage name would misleadingly conflate SPY's one-off backfill
        # with real daily price-ingestion cron health in job_runs.
        with JobRun(conn, stage="spy_price_backfill", run_type="manual", triggered_by=args.triggered_by) as job:
            count = backfill_spy_prices(conn, args.years)
            job.note(f"backfilled {count} SPY price bars")
        print(f"Backfilled {count} SPY price bars ({args.years} years).")
        return 0
    except Exception as exc:
        print(f"ERROR: SPY price backfill failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
