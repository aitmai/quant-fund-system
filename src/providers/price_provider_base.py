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
