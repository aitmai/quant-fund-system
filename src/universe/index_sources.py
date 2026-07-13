"""
Index constituent sources (DESIGN.md §3 Stage 1, §6.1).

Both sources are free, no-API-key iShares ETF holdings CSVs — NOT FMP.

FMP's S&P 500 constituent endpoint (/stable/sp500-constituent) turned out
to require a paid plan (confirmed via a live 402 Payment Required as of
2026-07-13), so this no longer touches FMP at all for universe sourcing.
Both indexes now use the same mechanism: the daily holdings CSV that each
iShares ETF publishes publicly.
  - S&P 500 -> IVV (iShares Core S&P 500 ETF)
  - Russell 1000 -> IWB (iShares Russell 1000 ETF)

This is fragile by nature — iShares can change a URL or CSV layout without
notice — so failures here are caught and logged rather than allowed to
kill the whole universe sync; each index sync independently. Treat both
as "best effort" until/unless a paid, contractual data source is worth
paying for.

Each function returns a list of dicts: {ticker, company_name, sector}.
Market cap / avg dollar volume filtering happens downstream in
construct_universe.py, once price_history exists to compute avg dollar
volume from (a fresh constituent list has no volume history yet on day one).
"""

import csv
import io
import os
import sys
from typing import Dict, List

import requests

REQUEST_TIMEOUT_SECONDS = 30

# iShares' site has been observed to reject/serve a non-CSV (HTML) response
# to requests that don't look like a real browser — same status 200, so
# raise_for_status() doesn't catch it, but there's no "Ticker" header row
# in the body. A standard browser User-Agent is the documented fix.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,text/plain,*/*",
}

# iShares holdings CSV URLs. iShares occasionally rotates these — override
# via the env vars below if either one 404s or stops parsing.
DEFAULT_IVV_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239726/ishares-core-sp-500-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IVV_holdings&dataType=fund"
)
DEFAULT_IWB_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)

_NON_EQUITY_TICKERS = {"-", "CASH"}


def _fetch_ishares_holdings_csv(url: str, index_label: str) -> List[Dict]:
    """Shared fetch+parse for any iShares ETF holdings CSV. Returns []
    (never raises) on any failure — network, missing header row, or
    unparseable layout — so one broken index source never blocks the
    other, or the rest of universe sync."""
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"WARNING: {index_label} holdings fetch failed, skipping: {exc}", file=sys.stderr)
        return []

    # iShares CSVs have several disclaimer/metadata rows before the real
    # table. Find the row that starts the actual holdings header (contains
    # "Ticker") rather than hardcoding a skiprows count, since that count
    # has been observed to vary between funds/exports.
    lines = resp.text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("Ticker")), None)
    if header_idx is None:
        # Print exactly what came back — a 200 with no "Ticker" row usually
        # means iShares served an HTML page (region gate, error page, etc.)
        # instead of the CSV, and this snippet is what's needed to diagnose
        # which, rather than guessing again blind.
        snippet = resp.text[:300].replace("\n", " ")
        print(
            f"WARNING: could not locate holdings header row in {index_label} CSV, skipping. "
            f"HTTP {resp.status_code}, content-type={resp.headers.get('Content-Type')}, "
            f"first 300 chars of body: {snippet!r}",
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
                continue  # skip futures/cash/other non-equity lines some exports include
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


def fetch_sp500_constituents() -> List[Dict]:
    url = os.environ.get("IVV_HOLDINGS_CSV_URL", DEFAULT_IVV_HOLDINGS_URL)
    return _fetch_ishares_holdings_csv(url, "S&P 500 (IVV)")


def fetch_russell1000_constituents() -> List[Dict]:
    url = os.environ.get("IWB_HOLDINGS_CSV_URL", DEFAULT_IWB_HOLDINGS_URL)
    return _fetch_ishares_holdings_csv(url, "Russell 1000 (IWB)")
