"""
Universe construction (DESIGN.md §3 Stage 1, §6.1).

Two entry points, both writing to the SAME universe/universe_changes
tables (tagged by source), per the manual+cron coexistence rule in
DESIGN.md §4:
  - sync_index_universe(): diffs current index constituents against
    what's already in `universe`, adding/deactivating as needed.
  - upload_manual_tickers(): adds a manually-curated ticker list.

Liquidity filtering (market cap floor, avg dollar volume floor) is kept
as a SEPARATE function (apply_liquidity_filters) rather than folded into
the sync, because avg_dollar_volume can't be computed for a brand-new
ticker until price_history has backfilled — see its docstring.

Known gap (flagging rather than silently guessing): `market_cap` has no
free bulk source at this stage — FMP's per-ticker /stable/profile endpoint
would give it, but that's a 4th API call per ticker competing with the
fundamentals budget, AND (learned 2026-07-13, after /stable/sp500-constituent
turned out to require a paid plan) it may itself be paid-gated on the free
tier — untested. Left NULL for now; market_cap enrichment is a good
candidate for a future lightweight pass, not wired into the daily budget
math yet.
"""

import sys
from datetime import date
from typing import Dict, Iterable, List, Optional, Set

from . import index_sources


def _fetch_selected_sources(include_sp500: bool, include_russell1000: bool) -> Dict[str, dict]:
    """Returns {ticker: {company_name, sector, source_index}}, deduped
    (Russell 1000 constituent info wins on overlap since it's the broader
    index and this repo treats S&P 500 as informational tagging only)."""
    combined: Dict[str, dict] = {}

    if include_sp500:
        for row in index_sources.fetch_sp500_constituents():
            combined[row["ticker"]] = {**row, "source_index": "sp500"}

    if include_russell1000:
        for row in index_sources.fetch_russell1000_constituents():
            combined[row["ticker"]] = {**row, "source_index": "russell1000"}

    return combined


def sync_index_universe(
    conn,
    include_sp500: bool = True,
    include_russell1000: bool = False,
    run_type: str = "cron",
    triggered_by: Optional[str] = None,
) -> dict:
    """Diff fetched index constituents against the current auto-sourced
    universe. Adds new tickers, deactivates ones that dropped out of the
    index. Never touches manually-added tickers (source='manual') even
    if they're absent from the index feeds."""
    fetched = _fetch_selected_sources(include_sp500, include_russell1000)
    fetched_tickers: Set[str] = set(fetched.keys())

    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticker FROM universe WHERE source = 'auto' AND is_active = TRUE")
            current_auto_tickers: Set[str] = {row[0] for row in cur.fetchall()}

    to_add = fetched_tickers - current_auto_tickers
    to_remove = current_auto_tickers - fetched_tickers
    today = date.today()

    with conn:
        with conn.cursor() as cur:
            for ticker in to_add:
                info = fetched[ticker]
                cur.execute(
                    """
                    INSERT INTO universe (ticker, company_name, sector, exchange, is_active, source, added_date, cik)
                    VALUES (%s, %s, %s, NULL, TRUE, 'auto', %s, %s)
                    ON CONFLICT (ticker) DO UPDATE SET
                        is_active = TRUE, source = 'auto',
                        company_name = COALESCE(EXCLUDED.company_name, universe.company_name),
                        sector = COALESCE(EXCLUDED.sector, universe.sector),
                        cik = COALESCE(EXCLUDED.cik, universe.cik)
                    """,
                    (ticker, info.get("company_name"), info.get("sector"), today, info.get("cik")),
                )
                cur.execute(
                    """
                    INSERT INTO universe_changes (ticker, change_type, change_date, source_index, detected_by, reason)
                    VALUES (%s, 'added', %s, %s, %s, %s)
                    """,
                    (ticker, today, info.get("source_index"), run_type, "index constituent sync"),
                )

            for ticker in to_remove:
                cur.execute(
                    "UPDATE universe SET is_active = FALSE WHERE ticker = %s AND source = 'auto'",
                    (ticker,),
                )
                cur.execute(
                    """
                    INSERT INTO universe_changes (ticker, change_type, change_date, source_index, detected_by, reason)
                    VALUES (%s, 'removed', %s, NULL, %s, %s)
                    """,
                    (ticker, today, run_type, "dropped from index constituent list"),
                )

            # Backfill cik for tickers that already existed before this
            # column existed (e.g. universe rows synced before the FMP ->
            # SEC EDGAR fundamentals switch). Only fills where currently
            # NULL — never overwrites a value that's already there.
            already_present = fetched_tickers & current_auto_tickers
            for ticker in already_present:
                cik = fetched[ticker].get("cik")
                if cik:
                    cur.execute(
                        "UPDATE universe SET cik = %s WHERE ticker = %s AND cik IS NULL",
                        (cik, ticker),
                    )

    return {"added": len(to_add), "removed": len(to_remove), "total_fetched": len(fetched_tickers)}


def upload_manual_tickers(
    conn, tickers: Iterable[Dict], run_type: str = "manual", triggered_by: Optional[str] = None
) -> dict:
    """`tickers` is an iterable of dicts with at least {"ticker": ...},
    optionally {"company_name": ..., "sector": ..., "exchange": ...}.
    Manually-added tickers are never auto-deactivated by sync_index_universe."""
    today = date.today()
    added = 0
    with conn:
        with conn.cursor() as cur:
            for row in tickers:
                ticker = row["ticker"].strip().upper()
                if not ticker:
                    continue
                cur.execute(
                    """
                    INSERT INTO universe (ticker, company_name, sector, exchange, is_active, source, added_date)
                    VALUES (%s, %s, %s, %s, TRUE, 'manual', %s)
                    ON CONFLICT (ticker) DO UPDATE SET
                        is_active = TRUE,
                        company_name = COALESCE(EXCLUDED.company_name, universe.company_name),
                        sector = COALESCE(EXCLUDED.sector, universe.sector),
                        exchange = COALESCE(EXCLUDED.exchange, universe.exchange)
                    """,
                    (ticker, row.get("company_name"), row.get("sector"), row.get("exchange"), today),
                )
                cur.execute(
                    """
                    INSERT INTO universe_changes (ticker, change_type, change_date, source_index, detected_by, reason)
                    VALUES (%s, 'added', %s, NULL, %s, %s)
                    """,
                    (ticker, today, run_type, "manual ticker upload"),
                )
                added += 1
    return {"added": added}


def apply_liquidity_filters(
    conn, min_avg_dollar_volume: float = 1_000_000, lookback_days: int = 60
) -> dict:
    """Deactivates any active ticker whose trailing-`lookback_days` average
    dollar volume (close * volume) is below `min_avg_dollar_volume`.

    Only evaluates tickers that already have at least `lookback_days` of
    price_history — a ticker mid-backfill is left alone rather than
    prematurely deactivated for "insufficient data," which would just
    mean re-adding it once its backfill catches up. Run this periodically
    (e.g. as part of the monthly universe sync) once backfill has had
    time to populate price_history.
    """
    today = date.today()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ticker, AVG(close * volume) AS avg_dollar_vol, COUNT(*) AS n
                FROM price_history
                WHERE date >= CURRENT_DATE - INTERVAL '%s days'
                GROUP BY ticker
                HAVING COUNT(*) >= %s
                """,
                (lookback_days, lookback_days // 2),  # tolerate some missing trading days
            )
            volumes = {row[0]: float(row[1]) for row in cur.fetchall() if row[1] is not None}

    deactivated = []
    with conn:
        with conn.cursor() as cur:
            for ticker, avg_dollar_vol in volumes.items():
                cur.execute(
                    "UPDATE universe SET avg_dollar_volume = %s WHERE ticker = %s",
                    (avg_dollar_vol, ticker),
                )
                if avg_dollar_vol < min_avg_dollar_volume:
                    cur.execute(
                        "UPDATE universe SET is_active = FALSE WHERE ticker = %s AND is_active = TRUE",
                        (ticker,),
                    )
                    cur.execute(
                        """
                        INSERT INTO universe_changes (ticker, change_type, change_date, source_index, detected_by, reason)
                        VALUES (%s, 'removed', %s, NULL, 'cron', %s)
                        """,
                        (ticker, today, f"avg dollar volume ${avg_dollar_vol:,.0f} below floor ${min_avg_dollar_volume:,.0f}"),
                    )
                    deactivated.append(ticker)

    return {"evaluated": len(volumes), "deactivated": len(deactivated), "deactivated_tickers": deactivated}
