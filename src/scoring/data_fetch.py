"""
Bulk data fetching for factor scoring (DESIGN.md §3 Stage 2).

Deliberately ONE query per table for the WHOLE universe, not one per
ticker — the same lesson learned the hard way in
src/providers/sec_edgar_provider.py, where a per-period query pattern
was doing 60-100+ round trips per ticker before being fixed to a single
prefetch. Factor scoring runs against the entire active universe every
week; looping a query per ticker here would repeat that mistake at
~500x the previous scale.
"""

from datetime import date, timedelta
from typing import Dict, Optional

import pandas as pd

# Momentum needs ~252 trading days back plus a ~21 trading day skip
# window; low-vol needs ~60. Calendar-day buffer is generous to comfortably
# cover both plus weekends/holidays without a second query.
PRICE_LOOKBACK_DAYS = 400


def fetch_active_universe(conn) -> pd.DataFrame:
    """Returns a DataFrame of {ticker, sector} for every active ticker.
    Sector is required for sector-relative z-scoring — tickers with a
    NULL sector (shouldn't normally happen for Wikipedia-sourced S&P 500
    rows, but possible for manually-added ones without one supplied)
    fall into a literal 'Unknown' bucket rather than being silently
    dropped, so they still get scored, just within a smaller group."""
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker, sector FROM universe WHERE is_active = TRUE")
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ticker", "sector"])
    df["sector"] = df["sector"].fillna("Unknown")
    return df


def fetch_price_panel(conn, as_of: Optional[date] = None) -> pd.DataFrame:
    """One query for the whole universe's recent price history. Returns
    columns [ticker, date, close, adj_close], sorted by ticker then date —
    ready to group by ticker for per-ticker momentum/low-vol computation."""
    as_of = as_of or date.today()
    window_start = as_of - timedelta(days=PRICE_LOOKBACK_DAYS)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, date, close, adj_close
                FROM price_history
                WHERE date >= %s AND date <= %s
                ORDER BY ticker, date ASC
                """,
                (window_start, as_of),
            )
            rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ticker", "date", "close", "adj_close"])
    # Prefer adjusted close (splits/dividends already applied); fall back
    # to raw close only where adj_close is missing.
    df["price"] = df["adj_close"].fillna(df["close"])
    return df


def fetch_latest_fundamentals(conn, as_of: Optional[date] = None) -> pd.DataFrame:
    """One query for every ticker's fundamentals HISTORY, then reduced to
    each ticker's most recent report as-of `as_of` — point-in-time
    correctness (DESIGN.md §8.2): a backtest re-running this for a past
    date must not see fundamentals filed after that date. Uses
    `filed_date`, not `report_date`, for that comparison — the date
    numbers were actually KNOWN to the market, not the period they
    describe."""
    as_of = as_of or date.today()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, report_date, filed_date, roe, ev_ebitda,
                       fcf_yield, debt_equity, earnings_variance
                FROM fundamentals
                WHERE filed_date <= %s
                ORDER BY ticker, filed_date ASC
                """,
                (as_of,),
            )
            rows = cur.fetchall()
    df = pd.DataFrame(
        rows,
        columns=["ticker", "report_date", "filed_date", "roe", "ev_ebitda", "fcf_yield", "debt_equity", "earnings_variance"],
    )
    if df.empty:
        return df
    # Keep only the latest known-as-of-`as_of` row per ticker.
    return df.groupby("ticker", as_index=False).tail(1).reset_index(drop=True)
