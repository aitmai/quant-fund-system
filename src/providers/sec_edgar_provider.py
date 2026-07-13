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

import bisect
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
# Some companies (confirmed: WMT) don't tag the combined `Liabilities`
# concept at all — only the split current/noncurrent pieces. Summed as a
# fallback when the combined tag comes back empty.
_LIABILITIES_CURRENT_TAGS = ("LiabilitiesCurrent",)
_LIABILITIES_NONCURRENT_TAGS = ("LiabilitiesNoncurrent",)
_CASH_TAGS = ("CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents")
_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
_DEPRECIATION_TAGS = ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation")
# EBITDA fallback when OperatingIncomeLoss isn't tagged at all (confirmed:
# WDAY — common for SaaS/tech companies whose income statement structure
# doesn't cleanly break out an "operating income" line). Bottom-up
# formula: NetIncome + Interest + Tax + D&A. Missing pieces default to 0
# rather than blocking the whole calc, same convention as depreciation
# above — only NetIncome is treated as required.
_INTEREST_EXPENSE_TAGS = ("InterestExpense", "InterestExpenseDebt", "InterestIncomeExpenseNet")
_INCOME_TAX_TAGS = ("IncomeTaxExpenseBenefit",)
_OPERATING_CASH_FLOW_TAGS = ("NetCashProvidedByUsedInOperatingActivities",)
_CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsForCapitalImprovements")
_EPS_TAGS = ("EarningsPerShareDiluted", "EarningsPerShareBasic")
_SHARES_OUTSTANDING_TAGS = ("CommonStockSharesOutstanding", "CommonStockSharesIssued")
# Shares outstanding is often only in the `dei` taxonomy, not `us-gaap`.
_SHARES_OUTSTANDING_DEI_TAGS = ("EntityCommonStockSharesOutstanding",)
# Last-resort fallback for multi-class-stock companies (confirmed gap:
# CVNA/Carvana has Class A + Class B stock and no combined point-in-time
# share count surfaced under any tag above — plausibly because EDGAR's
# companyfacts/companyconcept APIs only aggregate STANDARD-taxonomy facts,
# and a dual-class entity total may only exist as a company-specific
# custom-taxonomy extension, which these APIs don't expose at all; see
# https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
# Weighted-average diluted share count is a real, if approximate,
# substitute — it's the AVERAGE during the period rather than the
# point-in-time count, so market cap computed from it is an approximation,
# not exact. Better than nothing for otherwise-unavailable companies;
# flagged honestly rather than silently presented as equally precise.
_SHARES_OUTSTANDING_WEIGHTED_AVG_TAGS = (
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)


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
        if not liabilities_by_period:
            # Combined tag missing entirely (confirmed: WMT) — fall back to
            # summing the split current/noncurrent pieces. Only produces a
            # value for dates where at least one side is present; treats
            # a genuinely-missing side as 0 rather than dropping the period,
            # since many balance sheets legitimately have $0 in one of these.
            current = _collect_instant_facts(us_gaap, _LIABILITIES_CURRENT_TAGS)
            noncurrent = _collect_instant_facts(us_gaap, _LIABILITIES_NONCURRENT_TAGS)
            liabilities_by_period = _sum_period_dicts(current, noncurrent)
        cash_by_period = _collect_instant_facts(us_gaap, _CASH_TAGS)
        operating_income_by_period = _collect_duration_facts(us_gaap, _OPERATING_INCOME_TAGS)
        depreciation_by_period = _collect_duration_facts(us_gaap, _DEPRECIATION_TAGS)
        interest_expense_by_period = _collect_duration_facts(us_gaap, _INTEREST_EXPENSE_TAGS)
        income_tax_by_period = _collect_duration_facts(us_gaap, _INCOME_TAX_TAGS)
        operating_cash_flow_by_period = _collect_duration_facts(us_gaap, _OPERATING_CASH_FLOW_TAGS)
        capex_by_period = _collect_duration_facts(us_gaap, _CAPEX_TAGS)
        eps_by_period = _collect_duration_facts(us_gaap, _EPS_TAGS)
        shares_by_period = _collect_instant_facts(us_gaap, _SHARES_OUTSTANDING_TAGS)
        shares_source = "us-gaap"
        if not shares_by_period:
            shares_by_period = _collect_instant_facts(dei, _SHARES_OUTSTANDING_DEI_TAGS)
            shares_source = "dei"
        if not shares_by_period:
            # Weighted-average is a DURATION concept, not instant — approximates
            # a point-in-time count but isn't one. Used only because nothing
            # more precise was found; flagged via shares_source so it's
            # traceable if the resulting market cap looks off.
            shares_by_period = _collect_duration_facts(us_gaap, _SHARES_OUTSTANDING_WEIGHTED_AVG_TAGS)
            shares_source = "weighted-average (approximate)"
        if not shares_by_period:
            shares_source = "none found"

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

        # Prefetch this ticker's ENTIRE price history in ONE query, not one
        # per XBRL period — companies with 60-100+ quarters of filing
        # history were previously triggering that many separate round trips
        # to Supabase just for the market-cap join, which is genuinely slow
        # (confirmed 2026-07-13: this is what was making fundamentals runs
        # feel hung). See _lookup_market_cap() for the in-memory bisect
        # lookup this enables.
        price_index = _fetch_price_index(conn, ticker) if conn is not None else None

        results = []
        eps_history = []  # for trailing-window earnings_variance, built up in date order
        missing_field_counts = {"roe": 0, "debt_equity": 0, "ev_ebitda": 0, "fcf_yield": 0}
        # Diagnostic breakdown for WHY market cap (and therefore ev_ebitda/
        # fcf_yield) came back None — this is the ambiguity the original
        # warning couldn't resolve on its own; tracked precisely here so
        # one run gives a definitive answer instead of two guesses.
        market_cap_failure_reasons = {
            "no_shares_this_period": 0, "no_price_data": 0, "db_error": 0,
            "conn_not_provided": 0, "ok": 0,
        }
        # Market cap succeeding doesn't mean ev_ebitda/fcf_yield succeed —
        # both need additional inputs (operating income, liabilities, cash
        # flow). Tracked separately so "market cap was fine, something ELSE
        # was missing" (confirmed cases: WDAY, ICE, PODD) gets its own
        # diagnosis instead of silently falling through to the generic
        # "field was None for ALL periods" message with no explanation.
        ev_ebitda_failure_reasons = {"no_market_cap": 0, "no_operating_income": 0, "no_liabilities": 0, "ok": 0}
        fcf_yield_failure_reasons = {"no_market_cap": 0, "no_operating_cash_flow": 0, "ok": 0}

        for report_date_str in report_dates:
            net_income = net_income_by_period.get(report_date_str)
            equity = _nearest(equity_by_period, report_date_str)
            liabilities = _nearest(liabilities_by_period, report_date_str)
            cash = _nearest(cash_by_period, report_date_str)
            # NOTE (confirmed 2026-07-13, ~30 tickers across every sector):
            # these four used to look up via exact-match .get(report_date_str),
            # unlike equity/liabilities/cash above which use _nearest(). XBRL
            # concepts within the SAME filing don't always share identical
            # end-dates (dimensional/segment reporting quirks), so exact match
            # was systematically failing here even when the data existed a day
            # or two off — exactly why debt_equity (built from the _nearest()
            # fields) kept working while ev_ebitda/fcf_yield (built from these)
            # kept failing for every single period, with no company-specific
            # pattern to it.
            op_income = _nearest(operating_income_by_period, report_date_str)
            depreciation = _nearest(depreciation_by_period, report_date_str)
            op_cash_flow = _nearest(operating_cash_flow_by_period, report_date_str)
            capex = _nearest(capex_by_period, report_date_str)
            eps = _nearest(eps_by_period, report_date_str)
            shares = _nearest_shares(shares_by_period, report_date_str)

            roe = _safe_divide(net_income, equity)
            debt_equity = _safe_divide(liabilities, equity)  # broad definition: total liabilities / equity
            if roe is None:
                missing_field_counts["roe"] += 1
            if debt_equity is None:
                missing_field_counts["debt_equity"] += 1

            if conn is None:
                market_cap, cap_reason = None, "conn_not_provided"
            else:
                market_cap, cap_reason = _lookup_market_cap(price_index, report_date_str, shares)
            market_cap_failure_reasons[cap_reason] += 1

            interest_expense = _nearest(interest_expense_by_period, report_date_str)
            income_tax = _nearest(income_tax_by_period, report_date_str)

            ebitda = None
            if op_income is not None:
                ebitda = op_income + (depreciation or 0)
            elif net_income is not None:
                # Bottom-up fallback when OperatingIncomeLoss isn't tagged
                # at all (confirmed: WDAY, ICE, PODD — SaaS/tech companies
                # whose income statement doesn't cleanly break out an
                # "operating income" line). NetIncome + Interest + Tax +
                # D&A is a standard alternate EBITDA formula.
                ebitda = net_income + (interest_expense or 0) + (income_tax or 0) + (depreciation or 0)
            ev_ebitda = None
            if market_cap is not None and ebitda not in (None, 0) and liabilities is not None:
                enterprise_value = market_cap + liabilities - (cash or 0)
                ev_ebitda = _safe_divide(enterprise_value, ebitda)
            if ev_ebitda is None:
                missing_field_counts["ev_ebitda"] += 1
                if market_cap is None:
                    ev_ebitda_failure_reasons["no_market_cap"] += 1
                elif ebitda in (None, 0):
                    ev_ebitda_failure_reasons["no_operating_income"] += 1
                else:
                    ev_ebitda_failure_reasons["no_liabilities"] += 1
            else:
                ev_ebitda_failure_reasons["ok"] += 1

            fcf_yield = None
            if market_cap not in (None, 0) and op_cash_flow is not None:
                fcf = op_cash_flow - (capex or 0)
                fcf_yield = _safe_divide(fcf, market_cap)
            if fcf_yield is None:
                missing_field_counts["fcf_yield"] += 1
                if market_cap in (None, 0):
                    fcf_yield_failure_reasons["no_market_cap"] += 1
                else:
                    fcf_yield_failure_reasons["no_operating_cash_flow"] += 1
            else:
                fcf_yield_failure_reasons["ok"] += 1

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
        _warn_if_market_cap_mostly_failed(
            ticker, market_cap_failure_reasons, shares_source, len(results), shares_by_period, report_dates
        )
        _warn_if_ev_ebitda_mostly_failed(ticker, ev_ebitda_failure_reasons, len(results))
        _warn_if_fcf_yield_mostly_failed(ticker, fcf_yield_failure_reasons, len(results))
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


def _sum_period_dicts(a: Dict[str, float], b: Dict[str, float]) -> Dict[str, float]:
    """Sums two per-period dicts by date, treating a missing side as 0
    rather than dropping the period — used for the Liabilities =
    LiabilitiesCurrent + LiabilitiesNoncurrent fallback. Returns {} if
    BOTH inputs are empty (nothing to sum), so callers can still tell
    "no data at all" from "data present, summed to zero"."""
    if not a and not b:
        return {}
    dates = set(a.keys()) | set(b.keys())
    return {d: a.get(d, 0) + b.get(d, 0) for d in dates}


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


def _nearest_shares(by_period: Dict[str, float], target_date_str: str, max_days_tolerance: int = 45) -> Optional[float]:
    """Shares-outstanding specifically needs a DIFFERENT search direction
    than _nearest(): cover-page disclosures (dei:EntityCommonStockSharesOutstanding)
    are dated shortly AFTER the fiscal period end (confirmed against a live
    AAPL response: shares dated 2009-10-16 for a period ending 2009-09-26),
    not at/before it like true balance-sheet facts. _nearest()'s at-or-before
    logic systematically finds nothing for any company relying on this tag
    (confirmed real case: CVNA) — this searches both directions instead,
    picking whichever date is closest in absolute terms, within a tolerance
    window (share counts don't meaningfully change day-to-day, so a nearby
    date either direction is a reasonable stand-in)."""
    if not by_period:
        return None
    if target_date_str in by_period:
        return by_period[target_date_str]
    try:
        target = date.fromisoformat(target_date_str[:10])
    except ValueError:
        return None
    best_date_str, best_diff = None, None
    for d in by_period:
        try:
            d_date = date.fromisoformat(d[:10])
        except ValueError:
            continue
        diff = abs((d_date - target).days)
        if diff <= max_days_tolerance and (best_diff is None or diff < best_diff):
            best_diff, best_date_str = diff, d
    return by_period[best_date_str] if best_date_str is not None else None


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


def _fetch_price_index(conn, ticker: str):
    """ONE query for this ticker's entire price history, sorted ascending —
    replaces what used to be a separate query per XBRL period (60-100+
    round trips for companies with long filing histories). Returns
    (dates, closes) as parallel lists for bisect lookup, or None if the
    query itself raised (a real DB error, kept distinct from "no rows")."""
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT date, close FROM price_history
                    WHERE ticker = %s AND close IS NOT NULL
                    ORDER BY date ASC
                    """,
                    (ticker,),
                )
                rows = cur.fetchall()
    except Exception as exc:
        print(f"ERROR: price_history prefetch failed for {ticker}: {exc}", file=sys.stderr)
        return None
    dates = [d.isoformat() for d, _ in rows]
    closes = [float(c) for _, c in rows]
    return dates, closes


def _lookup_market_cap(price_index, report_date_str: str, shares_outstanding: Optional[float]):
    """In-memory nearest-at-or-before lookup against the prefetched price
    index — O(log n) via bisect instead of a database round trip.

    Returns (market_cap, reason). `reason` is one of:
      - "ok"                    market cap computed successfully
      - "no_shares_this_period" no shares-outstanding XBRL value for this
                                 specific period (may still have one for
                                 OTHER periods — this is per-period, not
                                 "this company has no shares data at all")
      - "no_price_data"         shares were available, but price_history
                                 has no row at or before this date for this
                                 ticker (common mid-backfill, or if the
                                 report date predates the backfill window)
      - "db_error"              the prefetch query raised — see the printed
                                 exception, since silently swallowing this
                                 would turn a real bug into "no data"
    """
    if not shares_outstanding:
        return None, "no_shares_this_period"
    if price_index is None:
        return None, "db_error"
    dates, closes = price_index
    if not dates:
        return None, "no_price_data"
    idx = bisect.bisect_right(dates, report_date_str) - 1
    if idx < 0:
        return None, "no_price_data"
    return closes[idx] * shares_outstanding, "ok"


def _warn_if_ev_ebitda_mostly_failed(ticker: str, reasons: dict, total_periods: int):
    """Covers the case market cap succeeds but ev_ebitda still fails for
    every period (confirmed: WDAY, ICE, PODD) — the market-cap-specific
    warning above correctly stays silent in that case (market cap DID
    work), which otherwise left this failure mode with no diagnosis at
    all beyond the generic 'field was None' message.

    IMPORTANT: prints a full breakdown as a fallback when no single reason
    accounts for literally every period — confirmed as the dominant real
    pattern (2026-07-13, ~30 tickers across every sector went completely
    silent here) since a company's 60-80 quarters of history very often
    fail for a MIX of reasons (e.g. price_history only covers the most
    recent ~3 years, so older periods fail on "no market cap" while
    recent ones fail on a missing tag) — no single reason ever hit 100%,
    so the single-reason checks below never fired, even though the field
    genuinely failed for every period. Silence was a diagnostic bug, not
    an absence of something to report.
    """
    if total_periods == 0 or reasons["ok"] > 0:
        return
    if reasons["no_market_cap"] == total_periods:
        return  # already covered by _warn_if_market_cap_mostly_failed
    if reasons["no_operating_income"] == total_periods:
        print(
            f"WARNING: ev_ebitda never computed for {ticker} — market cap was fine, but "
            f"neither OperatingIncomeLoss NOR the NetIncome+Interest+Tax+D&A fallback "
            f"produced a usable EBITDA for ANY period. This company's filings likely "
            f"don't tag NetIncomeLoss/ProfitLoss either, or EBITDA computed to exactly 0.",
            file=sys.stderr,
        )
    elif reasons["no_liabilities"] == total_periods:
        print(
            f"WARNING: ev_ebitda never computed for {ticker} — market cap and operating "
            f"income were fine, but no Liabilities value (combined or current+noncurrent "
            f"sum) found for ANY period.",
            file=sys.stderr,
        )
    else:
        print(
            f"WARNING: ev_ebitda never computed for {ticker} across all {total_periods} "
            f"periods, but no single reason dominates — likely different periods failing "
            f"for different reasons (e.g. older periods lacking market cap, recent ones "
            f"lacking a tag). Breakdown: {reasons}",
            file=sys.stderr,
        )


def _warn_if_fcf_yield_mostly_failed(ticker: str, reasons: dict, total_periods: int):
    """Same as _warn_if_ev_ebitda_mostly_failed, for fcf_yield's other
    input (operating cash flow) when market cap wasn't the problem. Same
    mixed-reasons fallback applies — see that function's docstring."""
    if total_periods == 0 or reasons["ok"] > 0:
        return
    if reasons["no_market_cap"] == total_periods:
        return  # already covered by _warn_if_market_cap_mostly_failed
    if reasons["no_operating_cash_flow"] == total_periods:
        print(
            f"WARNING: fcf_yield never computed for {ticker} — market cap was fine, but no "
            f"NetCashProvidedByUsedInOperatingActivities XBRL value found for ANY period. "
            f"This company's filings may tag operating cash flow under a different concept.",
            file=sys.stderr,
        )
    else:
        print(
            f"WARNING: fcf_yield never computed for {ticker} across all {total_periods} "
            f"periods, but no single reason dominates — likely different periods failing "
            f"for different reasons. Breakdown: {reasons}",
            file=sys.stderr,
        )


def _warn_if_market_cap_mostly_failed(
    ticker: str, reasons: dict, shares_source: str, total_periods: int,
    shares_by_period: Dict[str, float] = None, report_dates: list = None,
):
    """Resolves the ambiguity the original ev_ebitda/fcf_yield warning
    couldn't: prints exactly which failure mode dominated, so this is
    diagnosable from one run instead of two guesses."""
    if total_periods == 0 or reasons["ok"] > 0 or reasons["conn_not_provided"] == total_periods:
        return  # at least some periods worked, or conn was intentionally omitted — nothing to flag
    if reasons["no_shares_this_period"] == total_periods:
        if shares_by_period:
            # The tag WAS found (confirmed: CVNA) — so this isn't a
            # missing-concept problem, it's every period's date failing
            # to find a match. Print the actual date ranges so the
            # mismatch is visible instead of another guess.
            shares_dates = sorted(shares_by_period.keys())
            report_range = f"{min(report_dates)} to {max(report_dates)}" if report_dates else "unknown"
            print(
                f"WARNING: market cap never computed for {ticker} — shares-outstanding "
                f"tag WAS found ({shares_source}), but no per-period date match ever "
                f"succeeded. Shares data spans {shares_dates[0]} to {shares_dates[-1]}; "
                f"report dates span {report_range}. Likely a date-alignment issue "
                f"(possibly a multi-share-class company) rather than a missing tag.",
                file=sys.stderr,
            )
        else:
            print(
                f"WARNING: market cap never computed for {ticker} — no shares-outstanding "
                f"XBRL value found for ANY period (shares source checked: {shares_source}). "
                f"This company's filings may tag shares outstanding under a concept not in "
                f"_SHARES_OUTSTANDING_TAGS/_SHARES_OUTSTANDING_DEI_TAGS.",
                file=sys.stderr,
            )
    elif reasons["no_price_data"] == total_periods:
        print(
            f"WARNING: market cap never computed for {ticker} — shares outstanding WAS "
            f"found, but price_history has no matching rows for this ticker at any of "
            f"the {total_periods} report dates. Check price ingestion actually succeeded "
            f"for {ticker} (SELECT count(*) FROM price_history WHERE ticker = '{ticker}').",
            file=sys.stderr,
        )
    elif reasons["db_error"] > 0:
        print(
            f"WARNING: market cap lookup hit a database error for {ticker} on "
            f"{reasons['db_error']}/{total_periods} periods — see the ERROR line(s) above "
            f"for the actual exception.",
            file=sys.stderr,
        )
    else:
        # No single reason hit 100% of periods, but market cap still failed
        # for ALL of them (reasons["ok"] == 0) — a MIX of failure modes
        # across different periods (confirmed 2026-07-13: this was the
        # dominant real pattern across ~30 tickers spanning every sector,
        # e.g. older periods failing on price_history coverage while
        # different periods fail on shares-date alignment). Missing this
        # branch meant BOTH this function AND _warn_if_ev_ebitda_mostly_failed/
        # _warn_if_fcf_yield_mostly_failed stayed silent — those defer to
        # this one whenever their own "no_market_cap" reason hits 100%,
        # so a gap here was a gap everywhere ev_ebitda/fcf_yield depend on
        # market cap.
        print(
            f"WARNING: market cap never computed for {ticker} across all {total_periods} "
            f"periods, but no single reason dominates — different periods are failing for "
            f"different reasons (e.g. some too old for price_history's backfill window, "
            f"others failing shares-date alignment). Breakdown: {reasons}",
            file=sys.stderr,
        )


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
