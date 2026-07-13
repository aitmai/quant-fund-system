"""
SEC EDGAR fundamentals provider (DESIGN.md §5, §6.1 — swapped from FMP
2026-07-13 after FMP's free tier turned out to plan-gate ratios,
key-metrics, AND income-statement for essentially every S&P 500 ticker,
confirmed via live 402 responses — see fmp_fundamentals_provider.py's
module docstring for that history).

EDGAR's tradeoff, stated plainly: it's completely free with no plan
tiers, ever — but it hands you RAW FILED NUMBERS (net income, total
liabilities, shares outstanding, etc.), not pre-computed ratios like FMP
would have. Every ratio below is computed here from those raw numbers,
which means:
  - XBRL tagging is genuinely inconsistent company-to-company (a
    well-documented, widely-complained-about property of XBRL, not a
    bug in this code) — some concepts will be missing for some tickers.
    Each target value tries several candidate tags, same defensive
    pattern as FMP's field-name handling, and logs a warning (not a
    silent NULL) when nothing matches.
  - EV/EBITDA and FCF yield both need MARKET CAP (shares outstanding ×
    price), which means joining EDGAR's shares-outstanding figure
    against this system's own price_history — see _get_market_cap().
    If price_history has no data yet for a ticker (e.g. mid-backfill),
    those two fields come back None rather than blocking the whole row.

One API call (`companyfacts`) returns a company's ENTIRE XBRL history in
one shot — much better than FMP's 3-endpoints-per-ticker design. Rate
limit is 10 requests/second SEC-wide (not per-key — there is no key), so
pacing lives in fundamentals_ingestion.py the same way Yahoo throttling
pacing does for price ingestion.

Required: a descriptive User-Agent header naming who's making the
request (SEC's own fair-access policy, not a technicality to skip) — see
SEC_EDGAR_USER_AGENT in .env.example. Requests without one are commonly
rejected with 403.
"""

import os
import statistics
import sys
from datetime import date, datetime
from typing import Dict, List, Optional

import requests

from .price_provider_base import FundamentalsRow, PriceProviderError

EDGAR_BASE_URL = "https://data.sec.gov/api/xbrl/companyfacts"
REQUEST_TIMEOUT_SECONDS = 30

# Candidate XBRL concept tags per target value, tried in order — see
# module docstring re: inconsistent tagging across companies/taxonomies.
_NET_INCOME_TAGS = ("NetIncomeLoss", "ProfitLoss")
_EQUITY_TAGS = ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
_LIABILITIES_TAGS = ("Liabilities",)
_CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
_DEPRECIATION_TAGS = ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation")
_OPERATING_CASH_FLOW_TAGS = ("NetCashProvidedByUsedInOperatingActivities",)
_CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements")
_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
_SHARES_OUTSTANDING_TAGS = ("CommonStockSharesOutstanding", "CommonStockSharesIssued")
# Shares outstanding is often only in the `dei` taxonomy, not `us-gaap`.
_SHARES_OUTSTANDING_DEI_TAGS = ("EntityCommonStockSharesOutstanding",)


def _pad_cik(cik: str) -> str:
    return str(cik).strip().zfill(10)


class SECEdgarProvider:
    name = "sec_edgar"

    def __init__(self, user_agent: Optional[str] = None):
        self.user_agent = user_agent or os.environ.get("SEC_EDGAR_USER_AGENT")
        if not self.user_agent:
            raise PriceProviderError(
                "SEC_EDGAR_USER_AGENT is not set. See .env.example — SEC requires a "
                "descriptive User-Agent identifying who's making requests.",
                retryable=False,
            )

    def _fetch_company_facts(self, cik: str) -> dict:
        url = f"{EDGAR_BASE_URL}/CIK{_pad_cik(cik)}.json"
        try:
            resp = requests.get(url, headers={"User-Agent": self.user_agent}, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise PriceProviderError(f"EDGAR request failed for CIK {cik}: {exc}", retryable=True)

        if resp.status_code == 403:
            raise PriceProviderError(
                f"EDGAR returned 403 for CIK {cik} — check SEC_EDGAR_USER_AGENT is set "
                f"to a descriptive value (SEC rejects generic/missing User-Agents).",
                retryable=False,
            )
        if resp.status_code == 404:
            raise PriceProviderError(f"EDGAR has no company facts for CIK {cik} (404)", retryable=False)
        if resp.status_code == 429:
            raise PriceProviderError(f"EDGAR rate limit hit fetching CIK {cik}", retryable=True)
        if resp.status_code >= 500:
            raise PriceProviderError(f"EDGAR server error ({resp.status_code}) for CIK {cik}", retryable=True)
        if resp.status_code != 200:
            raise PriceProviderError(
                f"EDGAR returned {resp.status_code} for CIK {cik}: {resp.text[:200]}", retryable=False
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise PriceProviderError(f"EDGAR returned non-JSON for CIK {cik}: {exc}", retryable=True)

    def fetch_fundamentals(self, ticker: str, cik: str, conn=None) -> List[FundamentalsRow]:
        """One call to EDGAR's companyfacts endpoint, then a lot of
        merging: XBRL facts arrive as one flat list PER CONCEPT (not per
        period like FMP's rows), so this pivots by period end-date first,
        then computes each target ratio from whatever raw concepts landed
        in that period's bucket.

        `conn` is optional and only used for the market-cap join (EV/EBITDA,
        FCF yield need shares × price from this system's own price_history).
        Pass None to skip those two fields entirely (e.g. in isolated tests).
        """
        facts = self._fetch_company_facts(cik)
        us_gaap = facts.get("facts", {}).get("us-gaap", {})
        dei = facts.get("facts", {}).get("dei", {})

        net_income_by_period = _collect_duration_facts(us_gaap, _NET_INCOME_TAGS)
        equity_by_period = _collect_instant_facts(us_gaap, _EQUITY_TAGS)
        liabilities_by_period = _collect_instant_facts(us_gaap, _LIABILITIES_TAGS)
        cash_by_period = _collect_instant_facts(us_gaap, _CASH_TAGS)
        operating_income_by_period = _collect_duration_facts(us_gaap, _OPERATING_INCOME_TAGS)
        depreciation_by_period = _collect_duration_facts(us_gaap, _DEPRECIATION_TAGS)
        operating_cash_flow_by_period = _collect_duration_facts(us_gaap, _OPERATING_CASH_FLOW_TAGS)
        capex_by_period = _collect_duration_facts(us_gaap, _CAPEX_TAGS)
        eps_by_period = _collect_duration_facts(us_gaap, _EPS_TAGS)
        shares_by_period = _collect_instant_facts(us_gaap, _SHARES_OUTSTANDING_TAGS)
        if not shares_by_period:
            shares_by_period = _collect_instant_facts(dei, _SHARES_OUTSTANDING_DEI_TAGS)

        if not net_income_by_period and not equity_by_period and not eps_by_period:
            raise PriceProviderError(
                f"No usable XBRL facts found for {ticker} (CIK {cik}) — nothing to return.",
                retryable=False,
            )

        # Report dates: use net income's period ends as the anchor set —
        # it's the most universally-tagged duration concept and lines up
        # with how the `fundamentals` table's report_date/filed_date are
        # used downstream (point-in-time factor scoring, DESIGN.md §8.2).
        report_dates = sorted(net_income_by_period.keys()) or sorted(equity_by_period.keys())

        results = []
        eps_history = []  # for trailing-window earnings_variance, built up in date order
        missing_field_counts = {"roe": 0, "debt_equity": 0, "ev_ebitda": 0, "fcf_yield": 0}

        for report_date_str in report_dates:
            net_income = net_income_by_period.get(report_date_str)
            equity = _nearest(equity_by_period, report_date_str)
            liabilities = _nearest(liabilities_by_period, report_date_str)
            cash = _nearest(cash_by_period, report_date_str)
            op_income = operating_income_by_period.get(report_date_str)
            depreciation = depreciation_by_period.get(report_date_str)
            op_cash_flow = operating_cash_flow_by_period.get(report_date_str)
            capex = capex_by_period.get(report_date_str)
            eps = eps_by_period.get(report_date_str)
            shares = _nearest(shares_by_period, report_date_str)

            roe = _safe_divide(net_income, equity)
            debt_equity = _safe_divide(liabilities, equity)  # broad definition: total liabilities / equity
            if roe is None:
                missing_field_counts["roe"] += 1
            if debt_equity is None:
                missing_field_counts["debt_equity"] += 1

            market_cap = _get_market_cap(conn, ticker, report_date_str, shares) if conn is not None else None

            ebitda = None
            if op_income is not None:
                ebitda = op_income + (depreciation or 0)
            ev_ebitda = None
            if market_cap is not None and ebitda not in (None, 0) and liabilities is not None:
                enterprise_value = market_cap + liabilities - (cash or 0)
                ev_ebitda = _safe_divide(enterprise_value, ebitda)
            if ev_ebitda is None:
                missing_field_counts["ev_ebitda"] += 1

            fcf_yield = None
            if market_cap not in (None, 0) and op_cash_flow is not None:
                fcf = op_cash_flow - (capex or 0)
                fcf_yield = _safe_divide(fcf, market_cap)
            if fcf_yield is None:
                missing_field_counts["fcf_yield"] += 1

            if eps is not None:
                eps_history.append((report_date_str, eps))
            earnings_variance = _trailing_eps_stdev(eps_history, report_date_str)

            try:
                report_date_obj = datetime.fromisoformat(report_date_str[:10]).date()
            except ValueError:
                continue

            results.append(
                FundamentalsRow(
                    ticker=ticker,
                    report_date=report_date_obj,
                    filed_date=report_date_obj,  # EDGAR doesn't give a clean per-metric filed date in this view; see note below
                    roe=roe,
                    ev_ebitda=ev_ebitda,
                    fcf_yield=fcf_yield,
                    debt_equity=debt_equity,
                    earnings_variance=earnings_variance,
                )
            )

        _warn_if_mostly_missing(ticker, missing_field_counts, len(results))
        return results


def _collect_duration_facts(taxonomy: dict, candidate_tags: tuple) -> Dict[str, float]:
    """Duration concepts (income statement items) have start+end dates.
    Keyed by `end` date — the period's closing date, used as this
    system's report_date anchor."""
    for tag in candidate_tags:
        concept = taxonomy.get(tag)
        if not concept:
            continue
        by_period = {}
        for unit_values in concept.get("units", {}).values():
            for entry in unit_values:
                end = entry.get("end")
                val = entry.get("val")
                if end is not None and val is not None:
                    by_period[end] = val
        if by_period:
            return by_period
    return {}


def _collect_instant_facts(taxonomy: dict, candidate_tags: tuple) -> Dict[str, float]:
    """Instant concepts (balance sheet items) only have an `end` date
    (a point in time, not a period) — same dict shape as duration facts
    so both can be looked up the same way."""
    return _collect_duration_facts(taxonomy, candidate_tags)


def _nearest(by_period: Dict[str, float], target_date_str: str) -> Optional[float]:
    """Balance-sheet (instant) concepts don't always land on the exact
    same date as the income-statement anchor date — find the closest
    available date at or before the target rather than requiring an
    exact match, which would drop most rows for no good reason."""
    if not by_period:
        return None
    if target_date_str in by_period:
        return by_period[target_date_str]
    candidates = [d for d in by_period if d <= target_date_str]
    if not candidates:
        return None
    return by_period[max(candidates)]


def _safe_divide(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def _trailing_eps_stdev(eps_history: list, up_to_date_str: str) -> Optional[float]:
    """Same trailing-4-quarter EPS stdev proxy used in fmp_fundamentals_provider.py —
    kept identical so earnings_variance means the same thing regardless of
    which provider produced it."""
    window = [v for d, v in eps_history if d <= up_to_date_str][-4:]
    if len(window) < 2:
        return None
    return statistics.pstdev(window)


def _get_market_cap(conn, ticker: str, report_date_str: str, shares_outstanding: Optional[float]) -> Optional[float]:
    """market_cap = shares outstanding × closing price nearest (at or
    before) the report date, from this system's own price_history. Falls
    back to None (not an error) if price_history has no data yet for
    that ticker/date — common mid-backfill, shouldn't block the rest of
    the row's fields."""
    if not shares_outstanding or not conn:
        return None
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT close FROM price_history
                    WHERE ticker = %s AND date <= %s
                    ORDER BY date DESC LIMIT 1
                    """,
                    (ticker, report_date_str),
                )
                row = cur.fetchone()
    except Exception:
        return None
    if not row or row[0] is None:
        return None
    return float(row[0]) * shares_outstanding


def _warn_if_mostly_missing(ticker: str, missing_counts: dict, total_periods: int):
    if total_periods == 0:
        return
    for field_name, missing_count in missing_counts.items():
        if missing_count == total_periods:
            print(
                f"WARNING: EDGAR fundamentals field '{field_name}' was None for ALL "
                f"{total_periods} periods for {ticker}. Likely a missing/differently-tagged "
                f"XBRL concept for this company, or (for ev_ebitda/fcf_yield) no matching "
                f"price_history data to compute market cap from.",
                file=sys.stderr,
            )
