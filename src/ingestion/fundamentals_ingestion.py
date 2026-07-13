"""
Fundamentals ingestion — backfill then maintenance (DESIGN.md §5).

Switched from FMP to SEC EDGAR (2026-07-13) after FMP's free tier turned
out to plan-gate ratios, key-metrics, AND income-statement for
essentially every S&P 500 ticker — confirmed via live 402s, not a guess.
See src/providers/sec_edgar_provider.py's module docstring for the full
story and the tradeoffs of the swap.

Same backfill-then-maintenance pattern as price_ingestion.py, but:
  - Single provider (EDGAR) — no fallback needed; unlike FMP, there's no
    plan tier to hit a wall on.
  - calls_per_ticker = 1 (one companyfacts call returns a company's ENTIRE
    XBRL history at once — set in migrations/003_sec_edgar_fundamentals.sql).
  - Needs a CIK per ticker (universe.cik, sourced from Wikipedia's S&P 500
    table during universe sync) — tickers without one are skipped with a
    clear message rather than attempted and failed pointlessly.
  - Pacing (FUNDAMENTALS_REQUEST_DELAY_SECONDS) respects EDGAR's SEC-wide
    10 requests/second limit, same mechanism as price_ingestion.py's
    Yahoo-throttling pacing.
  - Maintenance staleness threshold is long (default 80 days) since
    fundamentals only change quarterly.
"""

import os
import sys
import time
from datetime import date, timedelta

from . import budget
from ..providers.price_provider_base import PriceProviderError
from ..providers.sec_edgar_provider import SECEdgarProvider

MAX_RETRY_COUNT = 3
STALENESS_DAYS = int(os.environ.get("FUNDAMENTALS_STALENESS_DAYS", "80"))
# EDGAR's real limit is 10 req/sec SEC-wide (not per-key — there's no
# key). Default here stays comfortably under that with margin, since an
# IP-level block from the SEC would be a much bigger problem than a slow
# ingestion run.
REQUEST_DELAY_SECONDS = float(os.environ.get("FUNDAMENTALS_REQUEST_DELAY_SECONDS", "0.2"))
# Same protective pattern as price_ingestion.py's MAX_CONSECUTIVE_FAILURES —
# kept as a safety net even though EDGAR has no plan-tier wall to hit;
# still useful if EDGAR itself has an outage or starts 429-ing broadly.
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
                SELECT s.ticker, s.retry_count, u.cik
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
        provider = SECEdgarProvider()
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
    skipped_no_cik = 0
    consecutive_failures = 0
    stopped_early_reason = ""
    cfg = budget.get_config(conn, "fundamentals")

    for ticker, retry_count, cik in candidates:
        if not cik:
            # A missing CIK means "we don't know how to look this ticker up
            # on EDGAR yet" — a data-completeness gap for THIS ticker, not
            # a signal about provider health. Doesn't count toward the
            # consecutive-failure streak, and costs no API call/budget.
            print(
                f"INFO: skipping {ticker} — no CIK on file (only Wikipedia-sourced "
                f"S&P 500 tickers get one automatically; manually-added tickers need "
                f"one supplied to look up EDGAR data).",
                file=sys.stderr,
            )
            mark_failure(conn, ticker, retry_count)
            skipped_no_cik += 1
            continue

        try:
            rows = provider.fetch_fundamentals(ticker, cik, conn=conn)
            upsert_fundamentals_rows(conn, rows)
            mark_success(conn, ticker, today)
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
                    f"{consecutive_failures} consecutive failures — possible EDGAR outage "
                    f"or broad rate-limiting, not bad tickers. Stopping early; remaining "
                    f"tickers will retry on the next scheduled run."
                )
                print(f"WARNING: {stopped_early_reason}", file=sys.stderr)
                break

        if REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

    summary = {
        "processed": succeeded + failed + skipped_no_cik,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_no_cik": skipped_no_cik,
        "skipped_reason": "",
        "stopped_early_reason": stopped_early_reason,
    }
    if job:
        job.note(
            f"processed={summary['processed']} succeeded={succeeded} "
            f"failed={failed} skipped_no_cik={skipped_no_cik}"
        )
        if stopped_early_reason:
            job.note(f"stopped_early: {stopped_early_reason}")
    return summary
