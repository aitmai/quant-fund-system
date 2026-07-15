"""
SPY price series fetch for hedge-sleeve volatility signals — Phase 9.

Deliberately point-in-time correct (as_of filtering, same discipline as
src/scoring/data_fetch.py) even though the hedge sleeve is currently
live-only — this keeps the function reusable if/when the hedge sleeve
ever gets backtested (DESIGN.md §12: gated behind the EODHD upgrade,
not a "never").
"""

from datetime import date
from typing import Optional

import pandas as pd

SPY_TICKER = "SPY"


def fetch_spy_price_series(conn, as_of: Optional[date] = None) -> pd.Series:
    """Returns SPY's adjusted-close price series (all available history
    up to and including as_of), indexed by date, ascending. Empty Series
    (not an exception) if SPY has no price_history rows at all — callers
    (volatility.py's functions) already treat "not enough history" as a
    None-returning condition, not a crash."""
    as_of = as_of or date.today()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, COALESCE(adj_close, close) AS price
            FROM price_history
            WHERE ticker = %s AND date <= %s
            ORDER BY date ASC
            """,
            (SPY_TICKER, as_of),
        )
        rows = cur.fetchall()
    if not rows:
        return pd.Series(dtype=float)
    df = pd.DataFrame(rows, columns=["date", "price"])
    df["price"] = df["price"].astype(float)
    return df.set_index("date")["price"]
