"""
Index constituent sources (DESIGN.md §3 Stage 1, §6.1).

  - S&P 500 -> Wikipedia's "List of S&P 500 companies" table. Free, no key,
    and — this matters — Wikipedia article pages don't sit behind the kind
    of bot-detection WAF that blocked the iShares approach (see below). This
    is the standard free source used across the quant-Python community for
    exactly this reason.
  - Russell 1000 -> iShares IWB ETF holdings CSV. Kept as best-effort, but
    flagged honestly: the equivalent S&P 500 CSV (IVV) was tried first and
    turned out to be blocked by iShares' bot detection — the response came
    back with a `Content-Type: text/csv` header but an actual HTML body
    (their normal website), which even a browser-like User-Agent didn't get
    past. Since IWB is served the same way, it's very likely blocked too.
    Treat --include-russell1000 as "try it, expect it might return nothing"
    rather than a reliable path — manual ticker upload
    (scripts/upload_manual_tickers.py) is the practical way to extend past
    S&P 500 for now.

Each function returns a list of dicts: {ticker, company_name, sector, cik}.
`cik` (SEC's Central Index Key, 10-digit zero-padded) is only populated
for the Wikipedia S&P 500 source — it's already a column in that table.
Needed for SEC EDGAR fundamentals lookups (src/providers/sec_edgar_provider.py).
Market cap / avg dollar volume filtering happens downstream in
construct_universe.py, once price_history exists to compute avg dollar
volume from (a fresh constituent list has no volume history yet on day one).
"""

import csv
import io
import os
import sys
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

REQUEST_TIMEOUT_SECONDS = 30

WIKIPEDIA_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"

# A descriptive User-Agent is Wikipedia's own etiquette guidance for
# automated access (see en.wikipedia.org/wiki/Wikipedia:User-Agent_policy) —
# unlike iShares, this isn't working around bot detection, it's being a
# polite, identifiable client.
_WIKIPEDIA_HEADERS = {
    "User-Agent": "quant-fund-system/1.0 (https://github.com/aitmai/quant-fund-system; universe sync)"
}

# iShares' site has been observed to reject/serve its normal website (HTML)
# to requests that don't look like a real browser, mislabeled with a CSV
# content-type. A browser User-Agent is the standard mitigation, though it
# did not resolve this for the IVV (S&P 500) endpoint — see module docstring.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,text/plain,*/*",
}

DEFAULT_IWB_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)

_NON_EQUITY_TICKERS = {"-", "CASH"}

# Column header text varies slightly across Wikipedia revisions
# ("Symbol" vs "Ticker symbol", etc.) — match by first hit among candidates
# rather than pinning to one exact string.
_SYMBOL_HEADER_CANDIDATES = ("symbol", "ticker symbol", "ticker")
_NAME_HEADER_CANDIDATES = ("security", "company", "name")
_SECTOR_HEADER_CANDIDATES = ("gics sector",)  # deliberately NOT "gics sub-industry"
_CIK_HEADER_CANDIDATES = ("cik",)  # SEC's Central Index Key — needed for EDGAR lookups


def fetch_sp500_constituents() -> List[Dict]:
    """Best-effort, like the other fetchers here: returns [] and logs a
    warning rather than raising, so a Wikipedia layout change doesn't take
    down the whole universe sync."""
    url = os.environ.get("SP500_WIKIPEDIA_URL", WIKIPEDIA_SP500_URL)
    try:
        resp = requests.get(url, headers=_WIKIPEDIA_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"WARNING: S&P 500 (Wikipedia) fetch failed, skipping: {exc}", file=sys.stderr)
        return []

    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.find("table", id="constituents")
        if table is None:
            # Fall back to the first sortable wikitable if the anchor id
            # ever changes — this page has historically used id="constituents"
            # but that's not guaranteed to be stable forever.
            table = soup.find("table", class_="wikitable")
        if table is None:
            print("WARNING: could not locate constituents table on Wikipedia page, skipping.", file=sys.stderr)
            return []

        rows = _parse_wikipedia_constituents_table(table)
        if not rows:
            print(
                "WARNING: found a table on the Wikipedia S&P 500 page but extracted zero rows — "
                "column headers may have changed. Check _SYMBOL_HEADER_CANDIDATES etc. in index_sources.py.",
                file=sys.stderr,
            )
        return rows
    except Exception as exc:
        print(f"WARNING: could not parse Wikipedia S&P 500 table, skipping: {exc}", file=sys.stderr)
        return []


def _find_header_index(headers: List[str], candidates: tuple) -> Optional[int]:
    lowered = [h.strip().lower() for h in headers]
    for candidate in candidates:
        for i, h in enumerate(lowered):
            if h == candidate:
                return i
    return None


def _parse_wikipedia_constituents_table(table) -> List[Dict]:
    header_cells = table.find("tr").find_all(["th", "td"])
    headers = [c.get_text(strip=True) for c in header_cells]

    symbol_idx = _find_header_index(headers, _SYMBOL_HEADER_CANDIDATES)
    name_idx = _find_header_index(headers, _NAME_HEADER_CANDIDATES)
    sector_idx = _find_header_index(headers, _SECTOR_HEADER_CANDIDATES)
    cik_idx = _find_header_index(headers, _CIK_HEADER_CANDIDATES)

    if symbol_idx is None:
        return []

    results = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if not cells or len(cells) <= symbol_idx:
            continue
        ticker = cells[symbol_idx].get_text(strip=True)
        if not ticker:
            continue
        # Wikipedia tickers sometimes use a dot for share classes (BRK.B);
        # normalize to the more common hyphen form used by most data
        # providers (BRK-B), since that's what price/fundamentals lookups
        # downstream expect. If a ticker legitimately has no dot, this is a no-op.
        ticker = ticker.replace(".", "-")

        cik = None
        if cik_idx is not None and len(cells) > cik_idx:
            raw_cik = cells[cik_idx].get_text(strip=True)
            if raw_cik.isdigit():
                # SEC's EDGAR API expects 10-digit zero-padded CIKs
                # (CIK0000320193.json) — normalize here once so every
                # downstream consumer (sec_edgar_provider.py) can assume
                # this shape rather than re-padding defensively everywhere.
                cik = raw_cik.zfill(10)

        results.append(
            {
                "ticker": ticker,
                "company_name": cells[name_idx].get_text(strip=True) if name_idx is not None and len(cells) > name_idx else None,
                "sector": cells[sector_idx].get_text(strip=True) if sector_idx is not None and len(cells) > sector_idx else None,
                "cik": cik,
            }
        )
    return results


def _fetch_ishares_holdings_csv(url: str, index_label: str) -> List[Dict]:
    """Shared fetch+parse for an iShares ETF holdings CSV. Returns []
    (never raises) on any failure. See module docstring re: this having
    been observed blocked for the IVV/S&P500 case — kept here for
    Russell 1000 on a best-effort basis only."""
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"WARNING: {index_label} holdings fetch failed, skipping: {exc}", file=sys.stderr)
        return []

    lines = resp.text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("Ticker")), None)
    if header_idx is None:
        snippet = resp.text[:300].replace("\n", " ")
        print(
            f"WARNING: could not locate holdings header row in {index_label} CSV, skipping "
            f"(likely blocked by iShares bot detection, same as observed for IVV — see "
            f"index_sources.py module docstring). HTTP {resp.status_code}, "
            f"content-type={resp.headers.get('Content-Type')}, first 300 chars: {snippet!r}",
            file=sys.stderr,
        )
        return []

    try:
        reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
        results = []
        for row in reader:
            ticker = (row.get("Ticker") or "").strip()
            if not ticker or ticker in _NON_EQUITY_TICKERS:
                continue
            asset_class = (row.get("Asset Class") or "").strip().lower()
            if asset_class and asset_class != "equity":
                continue
            results.append(
                {
                    "ticker": ticker,
                    "company_name": row.get("Name"),
                    "sector": row.get("Sector"),
                }
            )
        return results
    except Exception as exc:
        print(f"WARNING: could not parse {index_label} holdings CSV, skipping: {exc}", file=sys.stderr)
        return []


def fetch_russell1000_constituents() -> List[Dict]:
    url = os.environ.get("IWB_HOLDINGS_CSV_URL", DEFAULT_IWB_HOLDINGS_URL)
    return _fetch_ishares_holdings_csv(url, "Russell 1000 (IWB)")
