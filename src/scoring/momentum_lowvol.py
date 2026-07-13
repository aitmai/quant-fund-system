"""
Momentum and low-volatility raw factors (DESIGN.md §3 Stage 2).

Both computed from `price_history` alone, working on the bulk price
panel from data_fetch.fetch_price_panel() — grouped by ticker in memory,
never re-queried per ticker.

Momentum: 12-1 month convention — total return from ~12 months ago to
~1 month ago, deliberately EXCLUDING the most recent month (skips
short-term reversal noise, standard practice). Expressed in TRADING DAYS
(not calendar months) since that's what price_history actually gives us:
~252 trading days ≈ 12 months, ~21 trading days ≈ 1 month.

Low-volatility: inverse of trailing 60-trading-day realized volatility
(std dev of daily returns). Stored as the NEGATIVE of realized vol so a
HIGHER raw value always means "better" for this factor, consistent with
momentum/quality/value's sign convention before z-scoring — z-scoring a
mix of "higher raw = better" and "lower raw = better" columns without
normalizing signs first would silently invert the factor's meaning.
"""

from typing import Dict, Set, Tuple

import pandas as pd

MOMENTUM_LOOKBACK_TRADING_DAYS = 252
MOMENTUM_SKIP_TRADING_DAYS = 21
MOMENTUM_MIN_ROWS = MOMENTUM_LOOKBACK_TRADING_DAYS + 1  # need a price AT the lookback start too

LOWVOL_WINDOW_TRADING_DAYS = 60
LOWVOL_MIN_ROWS = LOWVOL_WINDOW_TRADING_DAYS + 1  # need N+1 prices for N returns


def compute_momentum_and_lowvol(price_panel: pd.DataFrame) -> Tuple[Dict[str, float], Dict[str, float], Set[str], Set[str]]:
    """Returns (momentum_raw, lowvol_raw, momentum_skipped, lowvol_skipped).
    The two skip-sets are tracked separately since a ticker can have
    enough history for one but not the other (low-vol needs far less)."""
    momentum_raw: Dict[str, float] = {}
    lowvol_raw: Dict[str, float] = {}
    momentum_skipped: Set[str] = set()
    lowvol_skipped: Set[str] = set()

    if price_panel.empty:
        return momentum_raw, lowvol_raw, momentum_skipped, lowvol_skipped

    for ticker, group in price_panel.groupby("ticker"):
        prices = group.sort_values("date")["price"].dropna().to_numpy()

        if len(prices) >= MOMENTUM_MIN_ROWS:
            price_1mo_ago = prices[-MOMENTUM_SKIP_TRADING_DAYS - 1]
            price_12mo_ago = prices[-MOMENTUM_LOOKBACK_TRADING_DAYS - 1]
            if price_12mo_ago > 0:
                momentum_raw[ticker] = (price_1mo_ago / price_12mo_ago) - 1
            else:
                momentum_skipped.add(ticker)
        else:
            momentum_skipped.add(ticker)

        if len(prices) >= LOWVOL_MIN_ROWS:
            recent = prices[-LOWVOL_MIN_ROWS:]
            returns = (recent[1:] / recent[:-1]) - 1
            std = returns.std(ddof=1)
            lowvol_raw[ticker] = -float(std)
        else:
            lowvol_skipped.add(ticker)

    return momentum_raw, lowvol_raw, momentum_skipped, lowvol_skipped
