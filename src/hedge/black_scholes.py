"""
Black-Scholes put delta + ~30-delta strike selection — Phase 9 (Hedge
Sleeve), DESIGN.md §3 fix #7.

yfinance's free SPY options chain gives strike/bid/ask/volume/open
interest/implied_vol but NOT Greeks — delta has to be computed in-house
to identify the ~30-delta put the design calls for. This module is pure
math (no I/O, no DB, no network) so it's fully unit-testable without a
live options chain — see tests/test_black_scholes.py.

Sign convention: put delta is reported as a NEGATIVE number in industry
convention (a put gains value as the underlying falls), but DESIGN.md's
"~30-delta put" language refers to the commonly-quoted MAGNITUDE (i.e.
what traders call "the 30 delta put" is delta ≈ -0.30). This module
returns the signed (negative) delta from the standard BS formula, and
`nearest_to_target_delta` compares against -abs(target_delta) so callers
can pass target_delta=0.30 in the conventional/positive-magnitude sense.
"""

import math
from dataclasses import dataclass
from typing import List, Optional


def put_delta(
    underlying_price: float,
    strike: float,
    time_to_expiration_years: float,
    implied_vol: float,
    risk_free_rate: float = 0.0,
) -> Optional[float]:
    """Standard Black-Scholes European put delta: N(d1) - 1, which is
    always in [-1, 0]. Returns None for degenerate inputs (zero/negative
    price, strike, time, or vol) rather than raising — a single bad
    options-chain row shouldn't crash a whole chain scan; callers filter
    None results the same way factor scoring treats missing z-scores."""
    S, K, T, sigma = underlying_price, strike, time_to_expiration_years, implied_vol
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    r = risk_free_rate
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) - 1.0


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function — avoids adding scipy
    as a dependency just for this one call (numpy/scipy are already
    project dependencies for other stages, but keeping this module
    zero-import-surface makes it trivially testable in isolation)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class PutQuote:
    """One row from an options chain, reduced to only what delta
    selection needs. `days_to_expiration` and `implied_vol` are assumed
    already computed/present on the chain row (yfinance's optionChain
    provides `impliedVolatility` directly per contract)."""

    strike: float
    days_to_expiration: int
    implied_vol: float
    bid: Optional[float] = None
    ask: Optional[float] = None

    @property
    def mid_premium(self) -> Optional[float]:
        """Mid of bid/ask — the standard premium estimate when no trade
        price is available. None if either side is missing/zero."""
        if self.bid is None or self.ask is None or self.bid <= 0 or self.ask <= 0:
            return None
        return (self.bid + self.ask) / 2.0


def nearest_to_target_delta(
    quotes: List[PutQuote],
    underlying_price: float,
    target_delta: float = 0.30,
    min_dte: int = 30,
    max_dte: int = 45,
    risk_free_rate: float = 0.0,
) -> Optional[PutQuote]:
    """Selects the put closest to DESIGN.md's ~30-delta, 30-45 DTE rule.

    Filters to the DTE window first (30-45), then picks whichever
    remaining strike's |delta| is closest to target_delta. Returns None
    if nothing in the chain falls inside the DTE window at all — this is
    a real "no eligible contract today" case (e.g. thin chain), not
    something to silently fall back on a wrong-DTE contract for."""
    in_window = [q for q in quotes if min_dte <= q.days_to_expiration <= max_dte]
    if not in_window:
        return None

    best_quote = None
    best_diff = None
    for q in in_window:
        delta = put_delta(
            underlying_price=underlying_price,
            strike=q.strike,
            time_to_expiration_years=q.days_to_expiration / 365.0,
            implied_vol=q.implied_vol,
            risk_free_rate=risk_free_rate,
        )
        if delta is None:
            continue
        diff = abs(abs(delta) - target_delta)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_quote = q
    return best_quote
