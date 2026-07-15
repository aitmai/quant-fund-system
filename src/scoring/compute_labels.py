"""
Training-label computation — Phase 3 (Stage 3 ML Ranking) backfill prep,
DESIGN.md §13 Phase 3.

Turns factor_scores rows (features — already point-in-time correct) into
(forward_return_pct, outperform_label) pairs (labels — only knowable in
hindsight, once the forward window has actually concluded). Deliberately
a separate module/table from factor_scores itself — see migrations/
004_training_labels.sql for the full rationale on why labels aren't
folded into factor_scores.decile_rank.

Label definition, decided 2026-07-14: outperform_label = 1 if a ticker's
realized forward return over HORIZON_TRADING_DAYS lands in the TOP
DECILE of that score_date's cross-sectional forward-return distribution,
else 0. Uses ACTUAL TRADING DAYS from price_history (not calendar days)
for the horizon — counting calendar days would silently shrink the real
window across weekends/holidays.

A label can only be computed once the forward window has concluded;
recent score_dates simply have no label yet. Re-running this later
naturally fills them in as more real price history accumulates — no
special "catch-up" logic needed, the LEFT JOIN in
_fetch_score_dates_needing_labels() just keeps finding them as pending
until then.
"""

import os
import time
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

HORIZON_TRADING_DAYS = int(os.environ.get("LABEL_HORIZON_TRADING_DAYS", "21"))
TOP_DECILE_THRESHOLD = float(os.environ.get("LABEL_TOP_DECILE_THRESHOLD", "0.90"))

# Upserts are committed in chunks rather than one giant transaction —
# a single executemany() across a full multi-year backfill (tens of
# thousands of rows) can exceed Supabase's pooler statement_timeout,
# and since it was all one transaction, a timeout rolled back EVERY
# row, not just the slow tail end. Chunking bounds each transaction's
# size so a timeout only costs the current chunk, and commits between
# chunks mean progress survives an interruption — a rerun's LEFT JOIN
# in _fetch_score_dates_needing_labels() naturally skips whatever
# already landed.
UPSERT_CHUNK_SIZE = int(os.environ.get("LABEL_UPSERT_CHUNK_SIZE", "500"))

# Raises statement_timeout for just the current transaction (SET LOCAL
# is scoped to the transaction, not the connection — safe even when
# DATABASE_URL points at Supabase's transaction-mode pooler, where the
# underlying physical connection is shared/reused across clients and a
# session-level SET would leak). Supabase's pooler otherwise applies a
# fairly short default statement_timeout that a bulk upsert can exceed
# even at a modest chunk size, especially with per-row network latency.
UPSERT_STATEMENT_TIMEOUT_MS = int(os.environ.get("LABEL_UPSERT_STATEMENT_TIMEOUT_MS", "120000"))

# A chunk can fail because the underlying TCP connection silently died
# (VPN blip, laptop sleep, idle NAT/firewall drop) rather than because
# the query itself was slow — that shows up server-side as the session
# sitting "idle in transaction, waiting on ClientRead" forever. Once
# src.db.get_connection()'s TCP keepalives detect that and raise, retry
# the SAME chunk on a fresh connection rather than losing the whole run.
UPSERT_MAX_RETRIES = int(os.environ.get("LABEL_UPSERT_MAX_RETRIES", "3"))
UPSERT_RETRY_DELAY_SECONDS = int(os.environ.get("LABEL_UPSERT_RETRY_DELAY_SECONDS", "5"))


def _fetch_score_dates_needing_labels(conn, horizon: int) -> pd.DataFrame:
    """Distinct (ticker, score_date) pairs from factor_scores that don't
    already have a training_labels row for this horizon. Reads distinct
    pairs regardless of which run_id produced the factor_scores row —
    the label is a pure function of price history, independent of which
    specific scoring run wrote the features for that date."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT fs.ticker, fs.score_date
            FROM factor_scores fs
            LEFT JOIN training_labels tl
              ON tl.ticker = fs.ticker AND tl.score_date = fs.score_date
              AND tl.horizon_trading_days = %s
            WHERE tl.ticker IS NULL
            ORDER BY fs.score_date, fs.ticker
            """,
            (horizon,),
        )
        rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["ticker", "score_date"])


def _fetch_price_history_for_labeling(conn, tickers, min_date: date, max_date: date) -> pd.DataFrame:
    """One bulk query for every ticker's price history across the whole
    range needed — same 'one query, not one per ticker' discipline as
    data_fetch.py."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticker, date, COALESCE(adj_close, close) AS price
            FROM price_history
            WHERE ticker = ANY(%s) AND date >= %s AND date <= %s
            ORDER BY ticker, date ASC
            """,
            (list(tickers), min_date, max_date),
        )
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ticker", "date", "price"])
    df["price"] = df["price"].astype(float)
    return df


def _forward_return_for_ticker(ticker_prices: Optional[pd.DataFrame], score_date: date, horizon: int) -> Optional[float]:
    """ticker_prices: that ticker's own price rows only, sorted by date,
    index reset to 0..n-1. Finds the nearest AT-OR-BEFORE score_date as
    the start observation (point-in-time correct — never uses a price
    from before it would have been known), then looks exactly `horizon`
    ROWS further in this ticker's own series (real trading days, not
    calendar days) for the end observation. Returns None if either side
    isn't available yet."""
    if ticker_prices is None or ticker_prices.empty:
        return None
    on_or_before = ticker_prices[ticker_prices["date"] <= score_date]
    if on_or_before.empty:
        return None
    start_idx = on_or_before.index[-1]
    end_idx = start_idx + horizon
    if end_idx >= len(ticker_prices):
        return None  # forward window hasn't concluded yet
    start_price = ticker_prices.loc[start_idx, "price"]
    end_price = ticker_prices.loc[end_idx, "price"]
    if pd.isna(start_price) or start_price == 0 or pd.isna(end_price):
        return None
    return (end_price - start_price) / start_price * 100.0


def compute_labels(conn, horizon: int = None) -> dict:
    """Computes and upserts training_labels for every (ticker, score_date)
    in factor_scores that doesn't already have one for this horizon.
    Returns a summary dict: candidates considered, how many got a real
    label, how many are still waiting on their forward window to
    conclude."""
    horizon = horizon if horizon is not None else HORIZON_TRADING_DAYS
    pending = _fetch_score_dates_needing_labels(conn, horizon)
    if pending.empty:
        return {"candidates": 0, "labeled": 0, "not_yet_computable": 0}

    tickers = pending["ticker"].unique().tolist()
    min_date = pending["score_date"].min() - timedelta(days=30)  # small buffer for "nearest at-or-before"
    max_date = date.today()  # can't need prices from the future
    price_df = _fetch_price_history_for_labeling(conn, tickers, min_date, max_date)
    price_by_ticker = {t: g.reset_index(drop=True) for t, g in price_df.groupby("ticker")}

    results = []
    for row in pending.itertuples(index=False):
        forward_return = _forward_return_for_ticker(
            price_by_ticker.get(row.ticker), row.score_date, horizon
        )
        results.append({"ticker": row.ticker, "score_date": row.score_date, "forward_return_pct": forward_return})

    results_df = pd.DataFrame(results)
    computable = results_df[results_df["forward_return_pct"].notna()].copy()
    not_yet_computable = len(results_df) - len(computable)

    if computable.empty:
        return {"candidates": len(results_df), "labeled": 0, "not_yet_computable": not_yet_computable}

    # Cross-sectional top-decile ranking WITHIN each score_date cohort —
    # "outperform" is always relative to that week's peer group, never an
    # absolute return threshold.
    computable["percentile_rank"] = computable.groupby("score_date")["forward_return_pct"].rank(pct=True)
    computable["outperform_label"] = (computable["percentile_rank"] >= TOP_DECILE_THRESHOLD).astype(int)

    _upsert_training_labels(conn, computable, horizon)
    return {"candidates": len(results_df), "labeled": len(computable), "not_yet_computable": not_yet_computable}


def _upsert_training_labels(conn, df: pd.DataFrame, horizon: int):
    rows = [
        (r.ticker, r.score_date, horizon, float(r.forward_return_pct), int(r.outperform_label))
        for r in df.itertuples(index=False)
    ]

    insert_sql = """
        INSERT INTO training_labels
            (ticker, score_date, horizon_trading_days, forward_return_pct, outperform_label)
        VALUES %s
        ON CONFLICT (ticker, score_date, horizon_trading_days)
        DO UPDATE SET forward_return_pct = EXCLUDED.forward_return_pct,
                      outperform_label = EXCLUDED.outperform_label,
                      computed_at = NOW()
    """

    total = len(rows)
    for start in range(0, total, UPSERT_CHUNK_SIZE):
        chunk = rows[start:start + UPSERT_CHUNK_SIZE]
        attempt = 0
        while True:
            try:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(f"SET LOCAL statement_timeout = '{UPSERT_STATEMENT_TIMEOUT_MS}'")
                        execute_values(cur, insert_sql, chunk)
                break
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                attempt += 1
                if attempt > UPSERT_MAX_RETRIES:
                    raise
                print(
                    f"  chunk at row {start} lost its connection ({exc.__class__.__name__}: {exc}); "
                    f"reconnecting and retrying (attempt {attempt}/{UPSERT_MAX_RETRIES})",
                    flush=True,
                )
                try:
                    conn.close()
                except Exception:
                    pass  # connection is already dead; nothing to clean up
                time.sleep(UPSERT_RETRY_DELAY_SECONDS)
                from src.db import get_connection
                conn = get_connection()
        print(
            f"  upserted {min(start + UPSERT_CHUNK_SIZE, total)}/{total} training_labels rows",
            flush=True,
        )
