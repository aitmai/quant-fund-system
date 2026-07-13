"""
Tiingo price provider.

Free tier (verify current limits before relying on them — Tiingo has
changed these over the years): ~50 requests/hour, 500/day. Requires
TIINGO_API_KEY. Chosen as one of the two backend options because it's a
documented, stable contract — a real fit for the budgeted-backfill
design in DESIGN.md §5, unlike yfinance's unofficial endpoint.
"""

import os
from datetime import date
from typing import List, Optional

import requests

from .price_provider_base import PriceBar, PriceProvider, PriceProviderError

TIINGO_BASE_URL = "https://api.tiingo.com/tiingo/daily"
REQUEST_TIMEOUT_SECONDS = 30


class TiingoProvider(PriceProvider):
    name = "tiingo"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("TIINGO_API_KEY")
        if not self.api_key:
            raise PriceProviderError(
                "TIINGO_API_KEY is not set. See .env.example.", retryable=False
            )

    def fetch_history(
        self, ticker: str, start_date: date, end_date: Optional[date] = None
    ) -> List[PriceBar]:
        end_date = end_date or date.today()
        url = f"{TIINGO_BASE_URL}/{ticker}/prices"
        params = {
            "startDate": start_date.isoformat(),
            "endDate": end_date.isoformat(),
            "format": "json",
            "resampleFreq": "daily",
            "token": self.api_key,
        }

        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise PriceProviderError(f"Tiingo request failed for {ticker}: {exc}", retryable=True)

        if resp.status_code == 404:
            raise PriceProviderError(f"Tiingo has no data for {ticker} (404)", retryable=False)
        if resp.status_code == 429:
            raise PriceProviderError(f"Tiingo rate limit hit fetching {ticker}", retryable=True)
        if resp.status_code >= 500:
            raise PriceProviderError(
                f"Tiingo server error ({resp.status_code}) fetching {ticker}", retryable=True
            )
        if resp.status_code != 200:
            raise PriceProviderError(
                f"Tiingo returned {resp.status_code} for {ticker}: {resp.text[:200]}",
                retryable=False,
            )

        try:
            rows = resp.json()
        except ValueError as exc:
            raise PriceProviderError(f"Tiingo returned non-JSON for {ticker}: {exc}", retryable=True)

        bars = []
        for row in rows:
            try:
                bars.append(
                    PriceBar(
                        ticker=ticker,
                        date=date.fromisoformat(row["date"][:10]),
                        open=row.get("open"),
                        high=row.get("high"),
                        low=row.get("low"),
                        close=row.get("close"),
                        adj_close=row.get("adjClose"),
                        volume=row.get("volume"),
                    )
                )
            except (KeyError, ValueError) as exc:
                # Skip one malformed row rather than failing the whole ticker.
                continue

        return bars

    def estimate_response_bandwidth_mb(self, num_bars: int) -> float:
        """Rough estimate for the 30-day bandwidth cap tracking in
        ingestion_daily_usage. Tiingo JSON rows run ~150-200 bytes each;
        this is deliberately approximate — the real cap is enforced by
        Tiingo itself, this is just for our own budget dashboarding."""
        return (num_bars * 180) / (1024 * 1024)
