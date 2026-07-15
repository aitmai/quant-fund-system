"""
VIX futures provider — Phase 9 (Hedge Sleeve), DESIGN.md §3.

CBOE's own futures exchange (CFE) publishes free, no-auth daily
settlement CSVs at cdn.cboe.com — confirmed live 2026-07-15 (real URL:
.../VX/VX_2027-01-20.csv). This is the tradable front-month instrument
DESIGN.md's hedge sleeve calls for, which yfinance/Yahoo cannot supply
at all (Yahoo only carries spot-style VIX indices, confirmed via a live
smoke test the same day — see conversation notes).

CSV columns confirmed live: Trade Date, Futures, Open, High, Low, Close,
Settle, Change, Total Volume, EFP, Open Interest.

Real-data quirk (confirmed live, not assumed): Open/Close are frequently
0.0000 on thin-trading days even though the contract has a real
consensus price that day — SETTLE is the authoritative daily price for
VIX futures, not Close. Every price this module returns is Settle.
"""

from dataclasses import dataclass
from datetime import date
from io import StringIO
from typing import Optional, Tuple

import pandas as pd
import requests

from src.hedge.vix_futures_calendar import effective_sizing_contract, front_and_next_month_contracts

CBOE_VX_CSV_URL = "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_{expiration}.csv"
REQUEST_TIMEOUT_SECONDS = 30


class VixFuturesProviderError(Exception):
    """Raised when a VIX futures CSV can't be fetched or parsed. Kept
    distinct from PriceProviderError/OptionsProviderError since this is
    a third, separate failure domain — a bad VIX futures fetch shouldn't
    be conflated with either equity-price or SPY-options bookkeeping."""


@dataclass
class VixFuturesQuote:
    contract_expiration: date
    trade_date: date
    settle: float
    open_interest: Optional[int]


def fetch_contract_settle(contract_expiration: date) -> VixFuturesQuote:
    """Fetches the CBOE CSV for one contract (identified by its
    expiration date, per vix_futures_calendar.vix_futures_expiration)
    and returns its MOST RECENT trading day's settle price.

    Raises VixFuturesProviderError on any fetch/parse failure, or if the
    CSV comes back with zero rows (e.g. a contract that hasn't started
    trading yet, or a URL for a date that isn't actually a real
    expiration) — never silently returns a stale/zero price.
    """
    url = CBOE_VX_CSV_URL.format(expiration=contract_expiration.isoformat())
    try:
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        raise VixFuturesProviderError(
            f"Failed to fetch VIX futures CSV for {contract_expiration}: {exc}"
        )

    try:
        df = pd.read_csv(StringIO(response.text))
    except Exception as exc:
        raise VixFuturesProviderError(
            f"Failed to parse VIX futures CSV for {contract_expiration}: {exc}"
        )

    if df.empty:
        raise VixFuturesProviderError(
            f"VIX futures CSV for {contract_expiration} returned zero rows "
            f"(contract may not be listed yet)"
        )

    df["Trade Date"] = pd.to_datetime(df["Trade Date"]).dt.date
    latest = df.sort_values("Trade Date").iloc[-1]

    oi = latest.get("Open Interest")
    return VixFuturesQuote(
        contract_expiration=contract_expiration,
        trade_date=latest["Trade Date"],
        settle=float(latest["Settle"]),
        open_interest=None if pd.isna(oi) else int(oi),
    )


@dataclass
class TermStructureSnapshot:
    front_month: VixFuturesQuote
    next_month: VixFuturesQuote

    @property
    def is_contango(self) -> bool:
        """Contango: next-month priced ABOVE front-month — the normal,
        more common state (market pricing in more distant uncertainty).
        Backwardation (front > next) typically signals acute near-term
        stress and is the less common state."""
        return self.next_month.settle > self.front_month.settle

    @property
    def term_structure_signal(self) -> str:
        """Matches vol_hedge_state.term_structure_signal's TEXT column
        (DESIGN.md §6.3) — 'contango' or 'backwardation'. Equal prices
        (rare, but possible) are treated as contango (the default/benign
        state) rather than introducing a third value the schema doesn't
        define."""
        return "contango" if self.is_contango or self.next_month.settle == self.front_month.settle else "backwardation"

    @property
    def roll_yield_pct(self) -> float:
        """Percent difference between next- and front-month settle —
        the raw magnitude behind the contango/backwardation label, for
        roll-cost tracking (DESIGN.md: 'contango/backwardation drag from
        the roll is explicitly modeled — logged as a running cost')."""
        return (self.next_month.settle - self.front_month.settle) / self.front_month.settle * 100.0


def fetch_effective_sizing_quote(
    as_of: Optional[date] = None, roll_window_trading_days: int = 5
) -> Tuple[VixFuturesQuote, bool]:
    """The settle price for whichever contract should actually be used
    for hedge SIZING today — applies the 5-trading-day roll rule (see
    vix_futures_calendar.effective_sizing_contract), unlike
    fetch_term_structure()'s front/next, which deliberately stays on the
    pure calendar-nearest contracts for signal purposes regardless of
    the roll decision. Returns (quote, rolled)."""
    as_of = as_of or date.today()
    contract_exp, rolled = effective_sizing_contract(as_of, roll_window_trading_days)
    quote = fetch_contract_settle(contract_exp)
    return quote, rolled


def fetch_term_structure(as_of: Optional[date] = None) -> TermStructureSnapshot:
    """Front-month and next-month VIX futures settle prices as of
    `as_of` (defaults to today), for the term-structure signal DESIGN.md
    §3 calls for. Contract expirations are computed, not looked up, via
    vix_futures_calendar.front_and_next_month_contracts — see that
    module's docstring for why this is safe (verified against a real
    CBOE URL)."""
    as_of = as_of or date.today()
    front_exp, next_exp = front_and_next_month_contracts(as_of)

    front_quote = fetch_contract_settle(front_exp)
    next_quote = fetch_contract_settle(next_exp)

    return TermStructureSnapshot(front_month=front_quote, next_month=next_quote)
