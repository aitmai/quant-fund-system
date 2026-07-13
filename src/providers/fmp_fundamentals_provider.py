"""
FMP fundamentals provider (DESIGN.md §5, §6.1).

Free tier has no bulk endpoints, so populating the `fundamentals` table's
columns takes 3 separate per-ticker calls:
  - /stable/ratios?symbol={ticker}
  - /stable/key-metrics?symbol={ticker}
  - /stable/income-statement?symbol={ticker}

All three are requested with period=quarter&limit=N so a SINGLE call per
endpoint returns N quarters of history at once — this is what makes the
"~12 days to backfill 3,000 tickers" timeline in DESIGN.md §5 work: budget
is spent once per ticker (x3 endpoints), not once per ticker-quarter.

calls_per_ticker=3 is set in migrations/002_phase1_ingestion.sql to keep
the daily budget math (daily_budget / calls_per_ticker = tickers/day) honest.

NOTE ON FIELD NAMES: FMP migrated their whole API to this /stable/
structure in 2026 and reshuffled things along the way — some fields (e.g.
ROE) appear to have moved from `ratios` into `key-metrics` on some plans,
and some field names changed (`debtEquityRatio` -> `debtToEquity`). None
of this has been confirmed against a live response with a real API key,
so roe/debt_equity/ev_ebitda/fcf_yield are each looked up by trying
several candidate field names across BOTH endpoints (see _first_present),
and fetch_fundamentals() logs a loud warning (not a silent NULL) if a
field never matches for a ticker — check stderr/job_runs after the first
real run and tell me what it prints if so, so the candidate list can be
corrected to the exact names your account actually returns.
"""

import os
import statistics
import sys
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from .price_provider_base import PriceProviderError  # reuse the same exception shape

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
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


def _first_present(row: dict, *candidate_keys: str):
    """Return the value of the first candidate key present with a non-None
    value in `row`. Exists because FMP's stable-API field names for a given
    metric aren't fully confirmed here — see fetch_fundamentals docstring."""
    for key in candidate_keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _warn_if_field_missing(ticker: str, field_name: str, raw_rows: list, by_date: Dict[str, dict]):
    """If every merged row for this ticker has `field_name` as None, warn
    loudly (once per fetch) rather than let a wrong/renamed FMP field name
    silently produce a column of NULLs. Include the raw keys actually seen
    so this is fixable from the log line alone."""
    if not raw_rows:
        return
    values = [fields.get(field_name) for fields in by_date.values()]
    if values and all(v is None for v in values):
        seen_keys = sorted(set().union(*(row.keys() for row in raw_rows)))
        print(
            f"WARNING: fundamentals field '{field_name}' was None for ALL "
            f"{len(values)} periods fetched for {ticker}. FMP's stable-API field "
            f"name for this may differ from what's coded — actual keys seen in "
            f"the response: {seen_keys}",
            file=sys.stderr,
        )


class FMPFundamentalsProvider:
    name = "fmp"

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("FMP_API_KEY")
        if not self.api_key:
            raise PriceProviderError("FMP_API_KEY is not set. See .env.example.", retryable=False)

    def _get(self, path: str, ticker: str) -> list:
        # FMP's /stable/ endpoints take the ticker as a query param
        # (?symbol=AAPL), not a path segment (/AAPL) like the old
        # /api/v3/ endpoints did — those are now legacy and 403 on the
        # free tier as of mid-2026.
        url = f"{FMP_BASE_URL}/{path}"
        params = {"symbol": ticker, "period": "quarter", "limit": QUARTERS_PER_REQUEST, "apikey": self.api_key}
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
        """Three calls, merged by report period, into `fundamentals` rows.

        FMP's 2026 /stable/ migration didn't just move URLs — it also
        appears to have reshuffled which endpoint serves some fields (e.g.
        ROE looks like it moved from `ratios` into `key-metrics` for some
        accounts) and possibly renamed some fields (`debtEquityRatio` ->
        `debtToEquity`). Rather than commit to one exact field name I
        haven't been able to verify against a live response, each target
        field tries several candidate keys, checked across BOTH endpoints'
        rows for that date. If NONE of a field's candidates are ever
        present across a whole ticker's response, that's flagged via
        _warn_if_field_missing() so it surfaces as a loud warning in
        job_runs/logs rather than a silent column of NULLs.
        """
        ratios = self._get("ratios", ticker)
        key_metrics = self._get("key-metrics", ticker)
        income = self._get("income-statement", ticker)

        by_date: Dict[str, dict] = {}

        for row in ratios + key_metrics:
            d = row.get("date")
            if not d:
                continue
            bucket = by_date.setdefault(d, {})
            if bucket.get("roe") is None:
                bucket["roe"] = _first_present(row, "roe", "returnOnEquity")
            if bucket.get("debt_equity") is None:
                bucket["debt_equity"] = _first_present(row, "debtToEquity", "debtEquityRatio")
            if bucket.get("ev_ebitda") is None:
                bucket["ev_ebitda"] = _first_present(row, "evToEBITDA", "enterpriseValueOverEBITDA", "evOverEBITDA")
            if bucket.get("fcf_yield") is None:
                bucket["fcf_yield"] = _first_present(row, "freeCashFlowYield", "fcfYield")
            bucket.setdefault("filed_date", row.get("fillingDate") or row.get("date"))

        _warn_if_field_missing(ticker, "roe", ratios + key_metrics, by_date)
        _warn_if_field_missing(ticker, "debt_equity", ratios + key_metrics, by_date)
        _warn_if_field_missing(ticker, "ev_ebitda", ratios + key_metrics, by_date)
        _warn_if_field_missing(ticker, "fcf_yield", ratios + key_metrics, by_date)

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
