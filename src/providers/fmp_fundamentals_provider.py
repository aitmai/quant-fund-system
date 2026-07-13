"""
FMP fundamentals provider (DESIGN.md §5, §6.1).

Free tier has no bulk endpoints, so populating the `fundamentals` table's
columns takes 3 separate per-ticker calls:
  - /v3/ratios/{ticker}          -> roe, debt_equity
  - /v3/key-metrics/{ticker}     -> ev_ebitda, fcf_yield (freeCashFlowYield)
  - /v3/income-statement/{ticker}-> eps history, used to derive earnings_variance

All three are requested with period=quarter&limit=N so a SINGLE call per
endpoint returns N quarters of history at once — this is what makes the
"~12 days to backfill 3,000 tickers" timeline in DESIGN.md §5 work: budget
is spent once per ticker (x3 endpoints), not once per ticker-quarter.

calls_per_ticker=3 is set in migrations/002_phase1_ingestion.sql to keep
the daily budget math (daily_budget / calls_per_ticker = tickers/day) honest.
"""

import os
import statistics
import sys
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from .price_provider_base import PriceProviderError  # reuse the same exception shape

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
REQUEST_TIMEOUT_SECONDS = 30
QUARTERS_PER_REQUEST = 40  # ~10 years of quarterly history in one call


class FundamentalsRow:
    def __init__(self, ticker, report_date, filed_date, roe, ev_ebitda, fcf_yield, debt_equity, earnings_variance):
        self.ticker = ticker
        self.report_date = report_date
        self.filed_date = filed_date
        self.roe = roe
        self.ev_ebitda = ev_ebitda
        self.fcf_yield = fcf_yield
        self.debt_equity = debt_equity
        self.earnings_variance = earnings_variance


class FMPFundamentalsProvider:
    name = "fmp"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise PriceProviderError("FMP_API_KEY is not set. See .env.example.", retryable=False)

    def _get(self, path: str, ticker: str) -> list:
        url = f"{FMP_BASE_URL}/{path}/{ticker}"
        params = {"period": "quarter", "limit": QUARTERS_PER_REQUEST, "apikey": self.api_key}
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise PriceProviderError(f"FMP request failed for {ticker} ({path}): {exc}", retryable=True)

        if resp.status_code == 429:
            raise PriceProviderError(f"FMP rate limit hit fetching {ticker} ({path})", retryable=True)
        if resp.status_code >= 500:
            raise PriceProviderError(f"FMP server error ({resp.status_code}) for {ticker} ({path})", retryable=True)
        if resp.status_code != 200:
            raise PriceProviderError(
                f"FMP returned {resp.status_code} for {ticker} ({path}): {resp.text[:200]}",
                retryable=False,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise PriceProviderError(f"FMP returned non-JSON for {ticker} ({path}): {exc}", retryable=True)

        if isinstance(data, dict) and data.get("Error Message"):
            # FMP's way of saying "bad ticker" or "endpoint not available on your plan"
            raise PriceProviderError(f"FMP error for {ticker} ({path}): {data['Error Message']}", retryable=False)

        return data if isinstance(data, list) else []

    def fetch_fundamentals(self, ticker: str) -> List[FundamentalsRow]:
        """Three calls, merged by report period, into `fundamentals` rows."""
        ratios = self._get("ratios", ticker)
        key_metrics = self._get("key-metrics", ticker)
        income = self._get("income-statement", ticker)

        by_date: Dict[str, dict] = {}

        for row in ratios:
            d = row.get("date")
            if not d:
                continue
            by_date.setdefault(d, {})["roe"] = row.get("returnOnEquity")
            by_date.setdefault(d, {})["debt_equity"] = row.get("debtEquityRatio")
            by_date.setdefault(d, {})["filed_date"] = row.get("fillingDate") or row.get("date")

        for row in key_metrics:
            d = row.get("date")
            if not d:
                continue
            by_date.setdefault(d, {})["ev_ebitda"] = row.get("enterpriseValueOverEBITDA")
            by_date.setdefault(d, {})["fcf_yield"] = row.get("freeCashFlowYield")
            by_date.setdefault(d, {}).setdefault("filed_date", row.get("date"))

        # earnings_variance: rolling stdev of trailing 4 quarters' EPS,
        # a simple proxy for earnings stability (design doesn't pin down
        # an exact formula for this — flagging the assumption here so it's
        # easy to swap for something more precise, e.g. EPS surprise vs.
        # consensus, once an estimates data source is added).
        income_sorted = sorted(income, key=lambda r: r.get("date", ""))
        eps_values = [r.get("eps") for r in income_sorted if r.get("eps") is not None]
        for i, row in enumerate(income_sorted):
            d = row.get("date")
            if not d:
                continue
            window = eps_values[max(0, i - 3): i + 1]
            variance = statistics.pstdev(window) if len(window) >= 2 else None
            by_date.setdefault(d, {})["earnings_variance"] = variance
            by_date.setdefault(d, {}).setdefault("filed_date", row.get("fillingDate") or d)

        results = []
        for d, fields in by_date.items():
            try:
                report_date = datetime.fromisoformat(d[:10]).date()
                filed_raw = fields.get("filed_date") or d
                filed_date = datetime.fromisoformat(filed_raw[:10]).date()
            except ValueError:
                continue
            results.append(
                FundamentalsRow(
                    ticker=ticker,
                    report_date=report_date,
                    filed_date=filed_date,
                    roe=fields.get("roe"),
                    ev_ebitda=fields.get("ev_ebitda"),
                    fcf_yield=fields.get("fcf_yield"),
                    debt_equity=fields.get("debt_equity"),
                    earnings_variance=fields.get("earnings_variance"),
                )
            )
        return results
