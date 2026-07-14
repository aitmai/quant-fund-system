"""
One-off check: confirm which fields FMP's free tier actually returns on
income-statement, balance-sheet-statement, and cash-flow-statement, for
ONE ticker. Doesn't touch the database or ingestion pipeline — just prints
raw JSON so we can see real field names before building against assumptions.

Usage:
    python check_fmp_fields.py AAPL
"""

import json
import os
import sys

import requests

FMP_BASE_URL = "https://financialmodelingprep.com/stable"
ENDPOINTS = ("income-statement", "balance-sheet-statement", "cash-flow-statement")


def main():
    ticker = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    api_key = os.environ.get("FMP_API_KEY")
    if not api_key:
        print("FMP_API_KEY not set in environment. Load your .env first, e.g.:")
        print("  export $(grep -v '^#' .env | xargs)   # or however you load it")
        sys.exit(1)

    for path in ENDPOINTS:
        url = f"{FMP_BASE_URL}/{path}"
        params = {"symbol": ticker, "period": "quarter", "limit": 1, "apikey": api_key}
        resp = requests.get(url, params=params, timeout=30)
        print(f"\n{'='*70}\n{path}  ->  HTTP {resp.status_code}\n{'='*70}")

        if resp.status_code != 200:
            print(resp.text[:500])
            continue

        try:
            data = resp.json()
        except ValueError:
            print("Non-JSON response:", resp.text[:300])
            continue

        if not data:
            print("Empty response (no data returned for this ticker/plan).")
            continue

        row = data[0] if isinstance(data, list) else data
        print("Fields returned (key: value):")
        for k, v in row.items():
            print(f"  {k}: {v}")

        # Flag the specific fields the EV/EBITDA + FCF plan depends on
        print("\n--- Fields we specifically need ---")
        if path == "income-statement":
            for field in ("ebitda", "operatingIncome", "depreciationAndAmortization"):
                print(f"  {field}: {row.get(field, '<<< MISSING >>>')}")
        elif path == "balance-sheet-statement":
            for field in ("totalDebt", "cashAndCashEquivalents", "commonStockSharesOutstanding",
                          "totalDebtIncludingCapitalLeaseObligation"):
                print(f"  {field}: {row.get(field, '<<< MISSING >>>')}")
        elif path == "cash-flow-statement":
            for field in ("operatingCashFlow", "capitalExpenditure", "freeCashFlow"):
                print(f"  {field}: {row.get(field, '<<< MISSING >>>')}")


if __name__ == "__main__":
    main()
