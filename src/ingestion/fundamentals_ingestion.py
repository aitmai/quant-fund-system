"""
Fundamentals ingestion — backfill then maintenance (DESIGN.md §5).

Same backfill-then-maintenance pattern as price_ingestion.py, but:
  - Single provider (FMP) — no fallback, since it's the only free
    fundamentals source with the statement-level detail this needs.
  - calls_per_ticker = 3 (ratios, key-metrics, income-statement), set in
    migrations/002_phase1_ingestion.sql.
  - Each successful fetch returns FULL history in one shot (QUARTERS_PER_REQUEST
    in fmp_fundamentals_provider.py), so "backfill" for a ticker is really
    just "first successful fetch" — there's no incremental date range to
    manage like price_history has.
  - Maintenance staleness threshold is long (default 80 days) since
    fundamentals only change quarterly.
"""

import os
import sys
from datetime import date, timedelta

from . import budget
from ..providers.fmp_fundamentals_provider import FMPFundamentalsProvider
from ..providers.price_provider_base import PriceProviderError

MAX_RETRY_COUNT = 3
STALENESS_DAYS = int(os.environ.get("FUNDAMENTALS_STALENESS_DAYS", "80"))
# Same protective pattern as price_ingestion.py's MAX_CONSECUTIVE_FAILURES:
# if every endpoint turns out to be plan-gated (see fmp_fundamentals_provider.py's
# circuit breaker), every remaining ticker this run would fail too. Stop
# early rather than grinding through hundreds of guaranteed failures.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get("FUNDAMENTALS_MAX_CONSECUTIVE_FAILURES", "15"))


def seed_new_tickers(conn):
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_state (ticker, data_type, fetch_status, priority_rank)
                SELECT u.ticker, 'fundamentals', 'pending', 100
                FROM universe u
                WHERE u.is_active = TRUE
                ON CONFLICT (ticker, data_type) DO NOTHING
                """
            )


def get_candidates(conn, limit: int):
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.ticker, s.retry_count
                FROM ingestion_state s
                JOIN universe u ON u.ticker = s.ticker AND u.is_active = TRUE
                WHERE s.data_type = 'fundamentals'
                  AND (
                        (s.fetch_status IN ('pending', 'failed') AND s.retry_count < %s)
                     OR (s.fetch_status = 'complete' AND s.last_fetched_date < CURRENT_DATE - INTERVAL '%s days')
                  )
                ORDER BY s.priority_rank ASC, s.last_attempt_at ASC NULLS FIRST
                LIMIT %s
                """,
                (MAX_RETRY_COUNT, STALENESS_DAYS, limit),
            )
            return cur.fetchall()


def upsert_fundamentals_rows(conn, rows):
    if not rows:
        return
    with conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO fundamentals (ticker, report_date, filed_date, roe, ev_ebitda,
                                           fcf_yield, debt_equity, earnings_variance)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, report_date) DO UPDATE SET
                    filed_date = EXCLUDED.filed_date, roe = EXCLUDED.roe,
                    ev_ebitda = EXCLUDED.ev_ebitda, fcf_yield = EXCLUDED.fcf_yield,
                    debt_equity = EXCLUDED.debt_equity, earnings_variance = EXCLUDED.earnings_variance
                """,
                [
                    (r.ticker, r.report_date, r.filed_date, r.roe, r.ev_ebitda,
                     r.fcf_yield, r.debt_equity, r.earnings_variance)
                    for r in rows
                ],
            )


def mark_success(conn, ticker: str, as_of: date):
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE ingestion_state
                SET last_fetched_date = %s, fetch_status = 'complete',
                    last_attempt_at = NOW(), retry_count = 0
                WHERE ticker = %s AND data_type = 'fundamentals'
                """,
                (as_of, ticker),
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
                WHERE ticker = %s AND data_type = 'fundamentals'
                """,
                (new_retry_count, new_status, ticker),
            )


def run(conn, job=None) -> dict:
    seed_new_tickers(conn)

    tickers_remaining, reason = budget.remaining_ticker_budget(conn, "fundamentals")
    if tickers_remaining <= 0:
        msg = f"Fundamentals ingestion skipped: {reason}"
        print(msg, file=sys.stderr)
        if job:
            job.note(msg)
        return {"processed": 0, "succeeded": 0, "failed": 0, "skipped_reason": reason, "stopped_early_reason": ""}

    try:
        provider = FMPFundamentalsProvider()
    except PriceProviderError as exc:
        msg = f"Fundamentals ingestion skipped: {exc}"
        print(msg, file=sys.stderr)
        if job:
            job.note(msg)
        return {
            "processed": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_reason": str(exc),
            "stopped_early_reason": "",
        }

    candidates = get_candidates(conn, tickers_remaining)
    today = date.today()
    succeeded = 0
    failed = 0
    consecutive_failures = 0
    stopped_early_reason = ""
    cfg = budget.get_config(conn, "fundamentals")

    for ticker, retry_count in candidates:
        try:
            rows = provider.fetch_fundamentals(ticker)
            upsert_fundamentals_rows(conn, rows)
            mark_success(conn, ticker, today)
            # 3 calls consumed regardless of how many quarters came back.
            budget.record_usage(conn, "fundamentals", calls=cfg["calls_per_ticker"])
            succeeded += 1
            consecutive_failures = 0
        except PriceProviderError as exc:
            print(f"ERROR: fundamentals fetch failed for {ticker}: {exc}", file=sys.stderr)
            mark_failure(conn, ticker, retry_count)
            budget.record_usage(conn, "fundamentals", calls=cfg["calls_per_ticker"])
            failed += 1
            consecutive_failures += 1

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                stopped_early_reason = (
                    f"{consecutive_failures} consecutive failures — likely all FMP endpoints "
                    f"plan-gated or otherwise unavailable, not bad tickers. Stopping early; "
                    f"remaining tickers will retry on the next scheduled run."
                )
                print(f"WARNING: {stopped_early_reason}", file=sys.stderr)
                break

    summary = {
        "processed": succeeded + failed,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_reason": "",
        "stopped_early_reason": stopped_early_reason,
    }
    if job:
        job.note(f"processed={summary['processed']} succeeded={succeeded} failed={failed}")
        if stopped_early_reason:
            job.note(f"stopped_early: {stopped_early_reason}")
    return summary
