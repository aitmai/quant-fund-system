"""
One-off check: inspect a ticker's raw XBRL concept keys from SEC EDGAR to
diagnose why a specific field (e.g. shares-outstanding) isn't matching
any of the candidate tags the code currently looks for. Originally built
to investigate WMT (shares-outstanding failing on 69/73 periods, the
reverse pattern of every other ticker checked, where price data was the
bottleneck instead) — now takes any ticker as an argument.

Doesn't touch the database or ingestion pipeline. Needs no API key —
EDGAR is fully open, just requires a descriptive User-Agent. Resolves
ticker -> CIK automatically via SEC's public company_tickers.json
mapping, so no CIK needs to be looked up or hardcoded by hand. Loads
.env automatically (via python-dotenv, already a project dependency),
so SEC_EDGAR_USER_AGENT just needs to be set in your .env file — no
need to export it into the shell manually first.

Usage:
    python check_edgar_shares.py WMT
    python check_edgar_shares.py AAPL
"""

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL_TEMPLATE = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"


def resolve_cik(ticker: str, user_agent: str) -> str:
    """SEC's own ticker->CIK mapping — same source EDGAR itself uses,
    kept fresh by SEC (no third-party lookup needed). Returns the CIK
    zero-padded to 10 digits, as companyfacts' URL requires."""
    resp = requests.get(TICKER_MAP_URL, headers={"User-Agent": user_agent}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    ticker_upper = ticker.upper()
    for entry in data.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            return str(entry["cik_str"]).zfill(10)
    raise ValueError(f"Ticker '{ticker}' not found in SEC's company_tickers.json mapping.")


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "WMT"
    user_agent = os.environ.get("SEC_EDGAR_USER_AGENT")
    if not user_agent:
        print("SEC_EDGAR_USER_AGENT not set. Load your .env first (same var your ingestion uses).")
        sys.exit(1)

    print(f"Resolving CIK for {ticker}...")
    cik = resolve_cik(ticker, user_agent)
    print(f"{ticker} -> CIK {cik}")

    edgar_url = COMPANYFACTS_URL_TEMPLATE.format(cik=cik)
    resp = requests.get(edgar_url, headers={"User-Agent": user_agent}, timeout=30)
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:500])
        sys.exit(1)

    data = resp.json()
    us_gaap = data.get("facts", {}).get("us-gaap", {})
    dei = data.get("facts", {}).get("dei", {})

    print(f"\n=== us-gaap concepts containing 'Shares' ({ticker}) ===")
    for concept in sorted(us_gaap.keys()):
        if "Shares" in concept:
            units = us_gaap[concept].get("units", {})
            unit_keys = list(units.keys())
            sample_count = sum(len(v) for v in units.values())
            print(f"  {concept}  (units: {unit_keys}, total data points: {sample_count})")

    print(f"\n=== dei concepts containing 'Shares' ({ticker}) ===")
    for concept in sorted(dei.keys()):
        if "Shares" in concept:
            units = dei[concept].get("units", {})
            unit_keys = list(units.keys())
            sample_count = sum(len(v) for v in units.values())
            print(f"  {concept}  (units: {unit_keys}, total data points: {sample_count})")

    print("\n=== What the code currently looks for ===")
    print("  us-gaap: CommonStockSharesOutstanding, CommonStockSharesIssued")
    print("  dei:     EntityCommonStockSharesOutstanding")

    # Show a few sample data points for whatever tag turns out to exist,
    # so we can see the date format / instant-vs-duration shape directly.
    for concept in ("CommonStockSharesOutstanding", "CommonStockSharesIssued"):
        if concept in us_gaap:
            units = us_gaap[concept].get("units", {})
            for unit_name, points in units.items():
                print(f"\n--- Sample data points for us-gaap:{concept} ({unit_name}) ---")
                for p in points[:5]:
                    print(f"  {p}")


if __name__ == "__main__":
    main()
