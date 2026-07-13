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

KNOWN ISSUE (confirmed 2026-07-13, live run): `ratios` returned HTTP 402
for essentially every S&P 500 ticker, with two different messages —
"limit must be between 0 and 5" on some, "this value set for 'symbol' is
not available under your current subscription" on others. FMP's own plan
comparison copy says annual fundamentals/ratios are a Starter-plan (paid)
feature, not part of Basic/free — so this looks like a plan-tier gate,
not a tunable parameter. Two mitigations here:
  1. QUARTERS_PER_REQUEST dropped to 5 (the disclosed free-tier cap) —
     cheap to try, fixes the "limit" flavor of 402 if that's genuinely
     independent of the plan-tier gate for any tickers.
  2. Per-endpoint circuit breaker (see _unavailable_endpoints below) —
     once ANY ticker gets a plan-tier-flavored 402 on a given endpoint,
     that endpoint is skipped (no HTTP call at all) for every subsequent
     ticker THIS run. Stops burning quota and log noise on something
     already known to be blocked, and fetch_fundamentals() now returns
     whatever partial data succeeded rather than aborting the whole
     ticker if e.g. income-statement works but ratios doesn't.
If this keeps happening even after the limit reduction, it means `ratios`
truly isn't available on the current FMP plan — that's a real plan-upgrade
decision to make, not something more code can route around, since the
data has to come from somewhere.

NOTE ON FIELD NAMES: FMP migrated their whole API to this /stable/
structure in 2026 and reshuffled things along the way — some fields (e.g.
ROE) appear to have moved from `ratios` into `key-metrics` on some plans,
and some field names changed (`debtEquityRatio` -> `debtToEquity`). None
of this has been confirmed against a live response with a real API key,
so roe/debt_equity/ev_ebitda/fcf_yield are each looked up by trying
several candidate field names across BOTH endpoints (see _first_present),
and fetch_fundamentals() logs a loud warning (not a silent NULL) if a
field never matches for a ticker — check stderr/job_runs after a clean
run and tell me what it prints if so, so the candidate list can be
corrected to the exact names your account actually returns.
"""

import os
import statistics
import sys
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from .price_provider_base import FundamentalsRow, PriceProviderError  # reuse the same exception shape

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
REQUEST_TIMEOUT_SECONDS = 30
QUARTERS_PER_REQUEST = int(os.environ.get("FMP_QUARTERS_PER_REQUEST", "5"))

ENDPOINTS = ("ratios", "key-metrics", "income-statement")

# Module-level, per-process only — a fresh cron run tomorrow starts clean.
# Mirrors the same pattern used for Tiingo/yfinance rate limits in
# provider_factory.py.
_unavailable_endpoints: set = set()


def reset_circuit_breaker():
    """Clears the unavailable-endpoints set. For test isolation."""
    _unavailable_endpoints.clear()


def _looks_like_plan_gated(message: str) -> bool:
    lowered = message.lower()
    return any(
        phrase in lowered
        for phrase in ("premium query parameter", "upgrade your plan", "current subscription")
    )


def _first_present(row: dict, *candidate_keys: str):
    """Return the value of the first candidate key present with a non-None
    value in `row`. Exists because FMP's stable-API field names for a given
    metric aren't fully confirmed here — see module docstring."""
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
        if path in _unavailable_endpoints:
            # Already confirmed plan-gated this run — don't waste an HTTP
            # call (or quota) re-confirming what we already know.
            raise PriceProviderError(
                f"Skipping '{path}' for {ticker} — already found unavailable on this plan this run.",
                retryable=False,
            )

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
        if resp.status_code == 402:
            message = resp.text[:300]
            if _looks_like_plan_gated(message):
                _unavailable_endpoints.add(path)
                print(
                    f"WARNING: FMP endpoint '{path}' looks plan-gated on your current "
                    f"subscription (confirmed via {ticker}) — skipping it for every "
                    f"remaining ticker this run rather than repeating the same 402. "
                    f"This is a real plan-tier limitation, not something a code fix can "
                    f"route around; check your FMP plan if you need this data.",
                    file=sys.stderr,
                )
            raise PriceProviderError(f"FMP returned 402 for {ticker} ({path}): {message}", retryable=False)
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

    def _get_or_empty(self, path: str, ticker: str) -> list:
        """Like _get, but never lets one endpoint's failure take down the
        whole ticker — logs once and returns [] so fetch_fundamentals can
        still use whatever the OTHER endpoints returned."""
        try:
            return self._get(path, ticker)
        except PriceProviderError as exc:
            print(f"INFO: {path} unavailable for {ticker}, continuing with other endpoints: {exc}", file=sys.stderr)
            return []

    def fetch_fundamentals(self, ticker: str) -> List[FundamentalsRow]:
        """Three calls, merged by report period, into `fundamentals` rows.

        Each of the three endpoints is fetched independently — one being
        unavailable (plan-gated, rate-limited, etc.) no longer aborts the
        whole ticker. Only raises (letting the caller retry/mark-failed)
        if ALL THREE endpoints failed; a partial result is still useful
        and gets returned instead of being thrown away.

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
        ratios = self._get_or_empty("ratios", ticker)
        key_metrics = self._get_or_empty("key-metrics", ticker)
        income = self._get_or_empty("income-statement", ticker)

        if not ratios and not key_metrics and not income:
            raise PriceProviderError(
                f"All 3 FMP endpoints failed/unavailable for {ticker} — nothing to return.",
                retryable=False,
            )

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

        if ratios or key_metrics:
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
