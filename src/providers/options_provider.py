"""
SPY options chain provider — Phase 9 (Hedge Sleeve), DESIGN.md §3.

Live-only (no as_of parameter): yfinance's free options endpoint only
exposes the CURRENT chain, not historical point-in-time chains — see
DESIGN.md §3's "two-tier provider approach." This provider is therefore
usable for live daily hedge sizing only; historical hedge-sleeve
backtesting is explicitly deferred to the EODHD paid upgrade (§12).

Confirmed against a real chain (2026-07-14, SPY $751.83): yfinance's
`option_chain()` returns columns [contractSymbol, lastTradeDate, strike,
lastPrice, bid, ask, change, percentChange, volume, openInterest,
impliedVolatility, inTheMoney, contractSize, currency] — no expiration
or DTE column, which is why `days_to_expiration` is computed from the
expiration string used to fetch that specific chain, not read off a row.

Real-chain data quality notes (confirmed live, not assumed):
- bid/ask can be NaN (not just 0) for thin/no-market-maker strikes —
  handled by coercing NaN to None before building PutQuote, since
  PutQuote.mid_premium only checks for None/<=0, not NaN.
- Deep-ITM strikes can show bid/ask inversions relative to neighboring
  strikes (stale/no quotes) — harmless here since the ~30-delta target
  lives well away from deep ITM, but nothing in this module assumes
  monotonic bid/ask by strike.
- `contractSize` is a string ("REGULAR" for standard 100-share
  contracts, vs "NonStandard" after certain corporate actions) — this
  provider only supports REGULAR contracts for now; NonStandard rows
  are skipped rather than mis-sized.
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import yfinance as yf

from src.hedge.black_scholes import PutQuote, nearest_to_target_delta

MIN_DTE = 30
MAX_DTE = 45
TARGET_DELTA = 0.30


class OptionsProviderError(Exception):
    """Raised when the options chain can't be fetched or nothing usable
    is found. Distinct from PriceProviderError (src/providers/
    price_provider_base.py) since options and price ingestion are
    separate failure domains — a bad options fetch shouldn't be
    conflated with price-ingestion's retry/circuit-breaker bookkeeping."""


@dataclass
class SelectedPut:
    """What the daily hedge-sizing job actually needs to write to
    vol_hedge_state: spy_put_strike, spy_put_dte, spy_put_delta,
    spy_put_premium (DESIGN.md §6.3)."""

    strike: float
    days_to_expiration: int
    expiration_date: str
    delta: float
    premium: float
    underlying_price: float


def _expirations_in_dte_window(ticker_obj, as_of: date, min_dte: int, max_dte: int) -> List[str]:
    """Every expiration string from yfinance's own list that falls in
    [min_dte, max_dte] relative to as_of. Returns [] rather than raising
    when nothing qualifies today — a genuine "no eligible expiration"
    day is possible near options-listing-cycle gaps and the caller
    should be able to tell that apart from a fetch failure."""
    try:
        expirations = ticker_obj.options
    except Exception as exc:
        raise OptionsProviderError(f"Failed to fetch SPY options expirations: {exc}")

    in_window = []
    for exp_str in expirations:
        exp_date = date.fromisoformat(exp_str)
        dte = (exp_date - as_of).days
        if min_dte <= dte <= max_dte:
            in_window.append(exp_str)
    return in_window


def _quotes_for_expiration(ticker_obj, exp_str: str, as_of: date) -> List[PutQuote]:
    """One expiration's put chain, reduced to PutQuote rows. Skips
    NonStandard contracts and rows missing strike/impliedVolatility
    outright (can't compute delta without them) rather than raising —
    one bad row in a 100+ row chain shouldn't kill the whole fetch."""
    try:
        chain = ticker_obj.option_chain(exp_str)
    except Exception as exc:
        raise OptionsProviderError(f"Failed to fetch SPY option chain for {exp_str}: {exc}")

    dte = (date.fromisoformat(exp_str) - as_of).days
    quotes = []
    for row in chain.puts.itertuples(index=False):
        if getattr(row, "contractSize", "REGULAR") != "REGULAR":
            continue
        strike = getattr(row, "strike", None)
        iv = getattr(row, "impliedVolatility", None)
        if strike is None or iv is None or _is_nan(strike) or _is_nan(iv):
            continue
        bid = getattr(row, "bid", None)
        ask = getattr(row, "ask", None)
        quotes.append(
            PutQuote(
                strike=float(strike),
                days_to_expiration=dte,
                implied_vol=float(iv),
                bid=None if _is_nan(bid) else bid,
                ask=None if _is_nan(ask) else ask,
            )
        )
    return quotes


def _is_nan(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def select_spy_hedge_put(
    as_of: Optional[date] = None,
    target_delta: float = TARGET_DELTA,
    min_dte: int = MIN_DTE,
    max_dte: int = MAX_DTE,
    risk_free_rate: float = 0.0,
) -> SelectedPut:
    """Live SPY ~30-delta, 30-45 DTE put selection — DESIGN.md §3's
    concrete hedge instrument rule. Scans EVERY expiration in the DTE
    window (not just the nearest one), since the closest-to-target-delta
    strike can land at any of them depending on that day's vol surface —
    confirmed on a real chain where the 30-DTE expiration alone
    contained a strike within 0.0002 of the 0.30 target, but nothing
    guarantees that's always true, so all in-window expirations are
    pooled before picking the single best match.

    Raises OptionsProviderError if no expiration falls in the DTE
    window, or if nothing in the pooled chain has a valid delta/premium
    (e.g. every candidate strike's bid/ask was unusable) — the caller
    should treat this as a "couldn't size the hedge today" condition,
    not silently skip logging it.
    """
    as_of = as_of or date.today()
    spy = yf.Ticker("SPY")

    expirations = _expirations_in_dte_window(spy, as_of, min_dte, max_dte)
    if not expirations:
        raise OptionsProviderError(
            f"No SPY options expirations fall within {min_dte}-{max_dte} DTE as of {as_of}"
        )

    try:
        underlying_price = float(spy.history(period="1d")["Close"].iloc[-1])
    except Exception as exc:
        raise OptionsProviderError(f"Failed to fetch SPY underlying price: {exc}")

    all_quotes: List[PutQuote] = []
    for exp_str in expirations:
        all_quotes.extend(_quotes_for_expiration(spy, exp_str, as_of))

    # Filter to quotes with a usable premium BEFORE delta selection, not
    # after. nearest_to_target_delta() only reasons about delta — it has
    # no way to know a given strike is unpriceable — so a thin/NaN-quoted
    # strike could otherwise "win" the delta match and then fail one
    # step later with nothing usable to fall back to. Regression case:
    # a real live chain had bid=NaN on the actual closest-delta strike.
    priceable_quotes = [q for q in all_quotes if q.mid_premium is not None]

    chosen = nearest_to_target_delta(
        priceable_quotes,
        underlying_price=underlying_price,
        target_delta=target_delta,
        min_dte=min_dte,
        max_dte=max_dte,
        risk_free_rate=risk_free_rate,
    )
    if chosen is None:
        raise OptionsProviderError(
            f"No usable put found across {len(expirations)} in-window expirations "
            f"({len(all_quotes)} candidate quotes)"
        )

    premium = chosen.mid_premium
    if premium is None:
        raise OptionsProviderError(
            f"Selected strike {chosen.strike} ({chosen.days_to_expiration} DTE) "
            f"has no usable bid/ask to compute a premium"
        )

    from src.hedge.black_scholes import put_delta

    delta = put_delta(
        underlying_price=underlying_price,
        strike=chosen.strike,
        time_to_expiration_years=chosen.days_to_expiration / 365.0,
        implied_vol=chosen.implied_vol,
        risk_free_rate=risk_free_rate,
    )

    exp_str = next(
        e for e in expirations if (date.fromisoformat(e) - as_of).days == chosen.days_to_expiration
    )

    return SelectedPut(
        strike=chosen.strike,
        days_to_expiration=chosen.days_to_expiration,
        expiration_date=exp_str,
        delta=delta,
        premium=premium,
        underlying_price=underlying_price,
    )
