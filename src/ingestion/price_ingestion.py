"""
Price ingestion — backfill then maintenance (DESIGN.md §5).

Single code path for both phases, per the design: whether a ticker is in
"backfill" or "maintenance" mode falls out of its ingestion_state row,
not a separate branch of code.
  - Never attempted (last_fetched_date IS NULL)      -> full backfill fetch
  - pending/failed with retry_count < 3               -> retry (backfill or a
                                                         previously-failed maintenance fetch)
  - complete but stale (last_fetched_date < today-1)  -> incremental maintenance fetch
Newly added tickers (from universe sync) automatically re-enter backfill,
since they get a fresh ingestion_state row with no last_fetched_date.
"""

import os
import sys
from datetime import date, timedelta

from . import budget
from ..providers.price_provider_base import PriceProviderError
from ..providers.provider_factory import fetch_with_fallback

DEFAULT_BACKFILL_YEARS = int(os.environ.get("PRICE_BACKFILL_YEARS", "3"))
MAX_RETRY_COUNT = 3


def seed_new_tickers(conn):
    """Ensure every active universe ticker has a price ingestion_state row.
    New tickers (freshly added by universe sync) land here with no
    last_fetched_date, which is exactly what routes them into backfill."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_state (ticker, data_type, fetch_status, priority_rank)
                SELECT u.ticker, 'price', 'pending', 100
                FROM universe u
                WHERE u.is_active = TRUE
                ON CONFLICT (ticker, data_type) DO NOTHING
                """
            )


def get_candidates(conn, limit: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.ticker, s.last_fetched_date, s.retry_count
            FROM ingestion_state s
            JOIN universe u ON u.ticker = s.ticker AND u.is_active = TRUE
            WHERE s.data_type = 'price'
              AND (
                    (s.fetch_status IN ('pending', 'failed') AND s.retry_count < %s)
                 OR (s.fetch_status = 'complete' AND s.last_fetched_date < CURRENT_DATE - INTERVAL '1 day')
              )
            ORDER BY s.priority_rank ASC, s.last_attempt_at ASC NULLS FIRST
            LIMIT %s
            """,
            (MAX_RETRY_COUNT, limit),
        )
        return cur.fetchall()


def upsert_price_bars(conn, bars):
    if not bars:
        return
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


def mark_success(conn, ticker: str, as_of: date, provider_name: str):
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_state
                SET last_fetched_date = %s, fetch_status = 'complete',
                    last_attempt_at = NOW(), retry_count = 0, last_provider_used = %s
                WHERE ticker = %s AND data_type = 'price'
                """,
                (as_of, provider_name, ticker),
            )


def mark_failure(conn, ticker: str, retry_count: int):
    new_retry_count = retry_count + 1
    new_status = "failed" if new_retry_count >= MAX_RETRY_COUNT else "pending"
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_state
                SET retry_count = %s, fetch_status = %s, last_attempt_at = NOW()
                WHERE ticker = %s AND data_type = 'price'
                """,
                (new_retry_count, new_status, ticker),
            )


def run(conn, job=None) -> dict:
    """Process as many tickers as today's budget allows. Returns summary counts."""
    seed_new_tickers(conn)

    tickers_remaining, reason = budget.remaining_ticker_budget(conn, "price")
    if tickers_remaining <= 0:
        msg = f"Price ingestion skipped: {reason}"
        print(msg, file=sys.stderr)
        if job:
            job.note(msg)
        return {"processed": 0, "succeeded": 0, "failed": 0, "skipped_reason": reason}

    candidates = get_candidates(conn, tickers_remaining)
    today = date.today()
    succeeded = 0
    failed = 0

    for ticker, last_fetched_date, retry_count in candidates:
        start_date = (
            last_fetched_date + timedelta(days=1)
            if last_fetched_date
            else today - timedelta(days=365 * DEFAULT_BACKFILL_YEARS)
        )
        if start_date > today:
            # Already caught up (e.g. ran twice same day) — nothing to do, mark complete.
            mark_success(conn, ticker, today, "n/a")
            continue

        try:
            bars, provider_used = fetch_with_fallback(conn, ticker, start_date, today)
            upsert_price_bars(conn, bars)
            mark_success(conn, ticker, today, provider_used)
            budget.record_usage(conn, "price", calls=1)
            succeeded += 1
        except PriceProviderError as exc:
            print(f"ERROR: price fetch failed for {ticker}: {exc}", file=sys.stderr)
            mark_failure(conn, ticker, retry_count)
            budget.record_usage(conn, "price", calls=1)  # the attempt still cost a call
            failed += 1

    summary = {"processed": succeeded + failed, "succeeded": succeeded, "failed": failed, "skipped_reason": ""}
    if job:
        job.note(f"processed={summary['processed']} succeeded={succeeded} failed={failed}")
    return summary
