"""
yfinance price provider.

Free, no API key. Wraps Yahoo Finance's unofficial endpoint via the
`yfinance` package (already a pinned dependency for the hedge sleeve's
live options pricing — see DESIGN.md §5). No published rate limit, but
aggressive bulk pulls can get soft-blocked (HTTP 429-equivalent errors
surfaced by yfinance as empty frames or exceptions), so this treats
"empty result with no obvious reason" as retryable rather than a hard
failure, since it's frequently transient throttling rather than a real
missing ticker.
"""

from datetime import date, timedelta
from typing import List, Optional

import yfinance as yf

from .price_provider_base import PriceBar, PriceProvider, PriceProviderError


class YFinanceProvider(PriceProvider):
    name = "yfinance"

    def fetch_history(
        self, ticker: str, start_date: date, end_date: Optional[date] = None
    ) -> List[PriceBar]:
        end_date = end_date or date.today()
        # yfinance's `end` is exclusive of the given date in practice for daily
        # bars, so pad by one day to make end_date inclusive like Tiingo's API.
        fetch_end = end_date + timedelta(days=1)

        try:
            df = yf.Ticker(ticker).history(
                start=start_date.isoformat(),
                end=fetch_end.isoformat(),
                interval="1d",
                auto_adjust=False,
                actions=False,
            )
        except Exception as exc:
            raise PriceProviderError(f"yfinance request failed for {ticker}: {exc}", retryable=True)

        if df is None or df.empty:
            # Could be a genuinely delisted/bad ticker, or Yahoo throttling us.
            # Treat as retryable — the fallback provider (Tiingo) will tell us
            # definitively via its own 404 if the ticker really doesn't exist.
            raise PriceProviderError(
                f"yfinance returned no rows for {ticker} (delisted, bad symbol, or throttled)",
                retryable=True,
            )

        bars = []
        for idx, row in df.iterrows():
            try:
                bars.append(
                    PriceBar(
                        ticker=ticker,
                        date=idx.date(),
                        open=float(row["Open"]) if row["Open"] == row["Open"] else None,
                        high=float(row["High"]) if row["High"] == row["High"] else None,
                        low=float(row["Low"]) if row["Low"] == row["Low"] else None,
                        close=float(row["Close"]) if row["Close"] == row["Close"] else None,
                        adj_close=float(row["Adj Close"]) if row["Adj Close"] == row["Adj Close"] else None,
                        volume=int(row["Volume"]) if row["Volume"] == row["Volume"] else None,
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue

        return bars

    def estimate_response_bandwidth_mb(self, num_bars: int) -> float:
        """yfinance has no published bandwidth cap (unlike FMP), so this
        exists only for symmetry with TiingoProvider / dashboard totals."""
        return (num_bars * 150) / (1024 * 1024)
