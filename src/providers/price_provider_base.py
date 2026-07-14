"""
Price provider interface (DESIGN.md §5 — Data Providers).

Both concrete providers (Tiingo, yfinance) implement this same contract
so ingestion code never branches on which one is active — that decision
lives entirely in provider_factory.py, driven by ingestion_config.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import List, Optional


@dataclass
class PriceBar:
    ticker: str
    date: date
    open: Optional[float]
    high: Optional[float]
    low: Optional[float]
    close: Optional[float]
    adj_close: Optional[float]
    volume: Optional[int]


class PriceProviderError(Exception):
    """Raised when a provider fails to fetch data.

    `retryable=True` means the failure looks transient (rate limit,
    timeout, 5xx) and worth falling back to the other provider or
    retrying later. `retryable=False` means the failure looks permanent
    for this ticker (404 / delisted / bad symbol) and retrying with a
    different provider is unlikely to help, though we still try once —
    it's cheap and occasionally one provider covers a ticker the other
    doesn't.
    """

    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class AllProvidersUnavailableError(PriceProviderError):
    """Raised instead of a plain PriceProviderError when EVERY configured
    provider is circuit-broken for the rest of this run — i.e. the
    failure reflects the run's environment (both providers currently
    unusable: rate-limited, or repeatedly returning the "empty
    response"/blocked signature), not anything wrong with THIS specific
    ticker.

    This distinction matters to the caller (price_ingestion.py):
      - A normal PriceProviderError for one ticker is that ticker's own
        problem (bad symbol, genuinely delisted, one-off timeout) and
        should count against its retry_count as usual.
      - AllProvidersUnavailableError means this ticker never really got
        a fair attempt this run — every remaining ticker this run would
        fail identically — so it shouldn't be penalized with a retry
        count it didn't earn, and the run should stop immediately
        rather than burning through MAX_CONSECUTIVE_FAILURES one wasted
        HTTP call at a time.
    """

    def __init__(self, message: str):
        super().__init__(message, retryable=True)


class PriceProvider(ABC):
    name: str = "base"

    @abstractmethod
    def fetch_history(
        self, ticker: str, start_date: date, end_date: Optional[date] = None
    ) -> List[PriceBar]:
        """Return daily OHLCV bars for `ticker` in [start_date, end_date].

        end_date defaults to "today" in the provider implementation.
        Must raise PriceProviderError on failure — never return an empty
        list to signal failure, since an empty list is a valid (if
        unusual) response for a ticker with no bars in range.
        """
        raise NotImplementedError


class FundamentalsRow:
    """Shared output shape for fundamentals providers (FMP, SEC EDGAR).
    Kept here rather than duplicated in each provider module."""

    def __init__(self, ticker, report_date, filed_date, roe, ev_ebitda, fcf_yield, debt_equity, earnings_variance):
        self.ticker = ticker
        self.report_date = report_date
        self.filed_date = filed_date
        self.roe = roe
        self.ev_ebitda = ev_ebitda
        self.fcf_yield = fcf_yield
        self.debt_equity = debt_equity
        self.earnings_variance = earnings_variance
