"""
Index constituent sources (DESIGN.md §3 Stage 1, §6.1).

Two sources, both free:
  - S&P 500: FMP's /v3/sp500_constituent endpoint. Cheap (1 call) and we
    already hold an FMP key for fundamentals, so no new credential needed.
  - Russell 1000: no free structured API exists for this; the standard
    free route is the public holdings CSV that iShares publishes for its
    IWB ETF (which tracks the Russell 1000). This is fragile by nature —
    iShares can change the URL or CSV layout without notice — so failures
    here are caught and logged rather than allowed to kill the whole
    universe sync. Treat Russell 1000 sourcing as "best effort" until/unless
    a more stable source is worth paying for.

Each function returns a list of dicts: {ticker, company_name, sector}.
Market cap / avg dollar volume filtering happens downstream in
construct_universe.py, once price_history exists to compute avg dollar
volume from (a fresh constituent list has no volume history yet on day one).
"""

import csv
import io
import os
import sys
from typing import List, Dict

import requests

FMP_BASE_URL = "https://financialmodelingprep.com/api/v3"
REQUEST_TIMEOUT_SECONDS = 30

# iShares IWB (Russell 1000) holdings CSV. iShares occasionally rotates this
# URL — override via IWB_HOLDINGS_CSV_URL env var if it 404s.
DEFAULT_IWB_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239707/ishares-russell-1000-etf/"
    "1467271812596.ajax?fileType=csv&fileName=IWB_holdings&dataType=fund"
)


def fetch_sp500_constituents() -> List[Dict]:
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        raise RuntimeError("FMP_API_KEY is not set. See .env.example.")

    url = f"{FMP_BASE_URL}/sp500_constituent"
    resp = requests.get(url, params={"apikey": api_key}, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()

    return [
        {
            "ticker": row["symbol"],
            "company_name": row.get("name"),
            "sector": row.get("sector"),
        }
        for row in data
        if row.get("symbol")
    ]


def fetch_russell1000_constituents() -> List[Dict]:
    """Best-effort. Returns [] and logs a warning on any failure rather
    than raising, so a broken iShares URL doesn't block the whole sync —
    S&P 500 sourcing still runs fine on its own."""
    url = os.environ.get("IWB_HOLDINGS_CSV_URL", DEFAULT_IWB_HOLDINGS_URL)
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"WARNING: Russell 1000 holdings fetch failed, skipping: {exc}", file=sys.stderr)
        return []

    # iShares CSVs have several header/disclaimer rows before the real table.
    # Find the row that starts the actual holdings header (contains "Ticker").
    lines = resp.text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.strip().startswith("Ticker")), None)
    if header_idx is None:
        print("WARNING: could not locate holdings header row in IWB CSV, skipping.", file=sys.stderr)
        return []

    try:
        reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
        results = []
        for row in reader:
            ticker = (row.get("Ticker") or "").strip()
            if not ticker or ticker in ("-", "CASH"):
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
        print(f"WARNING: could not parse IWB holdings CSV, skipping: {exc}", file=sys.stderr)
        return []
