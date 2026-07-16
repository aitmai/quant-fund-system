"""
Ticker -> sector lookup helper — supports the manual ticker upload
feature (DESIGN.md §3 Stage 1, §6.1 universe.source='manual').

Takes a plain file of tickers (no other columns — just tickers, one per
line and/or comma-separated, both accepted) and produces a
ticker,company_name,sector CSV, using yfinance's `.info` as a best-effort
enrichment source.

This is a REVIEW step, not an auto-upload: it deliberately writes a CSV
for you to look over (and hand-correct if needed) rather than writing
straight to the universe table. Two reasons this matters:
  1. yfinance's sector field is Yahoo's own taxonomy, not GICS (the
     classification the S&P 500 path pulls from Wikipedia) — labels
     like "Technology" (Yahoo) vs "Information Technology" (GICS) can
     describe the same real sector without matching as text, which
     would quietly break Stage 4's sector-cap grouping if trusted
     blindly. Reviewing the output lets you normalize labels by hand
     before they ever reach Stage 4's correlation/sector-cap filter.
  2. yfinance's `.info` endpoint is unofficial and occasionally returns
     None/missing fields even for valid, liquid tickers — worth a human
     glance rather than silently uploading gaps.

Output columns match scripts/upload_manual_tickers.py's expected CSV
format exactly (ticker,company_name,sector,exchange — exchange left
blank here, not fetched), so the output of this script can be edited
and fed directly into that script:

    python scripts/lookup_ticker_sectors.py --file my_tickers.txt
    # review/edit the output CSV, then:
    python scripts/upload_manual_tickers.py --file my_tickers_with_sectors.csv

Input file format: accepts EITHER one ticker per line, OR
comma-separated on one or more lines, OR a mix of both — every ticker
gets extracted regardless of which style you used.

Usage:
    python scripts/lookup_ticker_sectors.py --file my_tickers.txt
    python scripts/lookup_ticker_sectors.py --file my_tickers.txt --output enriched.csv
"""

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, ".")

import yfinance as yf

from src.universe.construct_universe import parse_bare_ticker_list

REQUEST_DELAY_SECONDS = float(os.environ.get("SECTOR_LOOKUP_REQUEST_DELAY_SECONDS", "0.5"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("SECTOR_LOOKUP_REQUEST_TIMEOUT_SECONDS", "15"))


def parse_ticker_file(path: str):
    """Reads a file and delegates to the shared parse_bare_ticker_list()
    (src/universe/construct_universe.py) so this script and the GUI's
    bulk-upload route always accept identical input formats."""
    with open(path) as f:
        return parse_bare_ticker_list(f.read())


def lookup_sector_and_name(ticker: str):
    """Best-effort — returns (company_name, sector), either of which may
    be None if yfinance's .info doesn't have it or the request fails.
    Never raises: a bad/delisted ticker shouldn't stop the rest of the
    file from being processed, matching the same best-effort pattern
    used throughout this codebase's other external-data fetchers (e.g.
    src/universe/index_sources.py's fetch functions)."""
    try:
        info = yf.Ticker(ticker).info
    except Exception as exc:
        print(f"WARNING: yfinance lookup failed for {ticker}: {exc}", file=sys.stderr)
        return None, None

    if not info:
        print(f"WARNING: yfinance returned no data for {ticker} (possibly delisted or invalid)", file=sys.stderr)
        return None, None

    company_name = info.get("longName") or info.get("shortName")
    sector = info.get("sector")
    if not sector:
        print(f"WARNING: no sector found for {ticker} — leave blank or fill in by hand before uploading", file=sys.stderr)
    return company_name, sector


def main():
    parser = argparse.ArgumentParser(description="Look up company name + sector for a plain ticker list.")
    parser.add_argument("--file", required=True, help="Path to a text file of tickers (one per line and/or comma-separated)")
    parser.add_argument("--output", default=None, help="Output CSV path. Defaults to <input-name>_with_sectors.csv")
    args = parser.parse_args()

    try:
        tickers = parse_ticker_file(args.file)
    except FileNotFoundError:
        print(f"ERROR: file not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    if not tickers:
        print(f"ERROR: no tickers found in {args.file}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        output_path = args.output
    else:
        base, _ = os.path.splitext(args.file)
        output_path = f"{base}_with_sectors.csv"

    rows = []
    found_sector_count = 0
    for i, ticker in enumerate(tickers):
        company_name, sector = lookup_sector_and_name(ticker)
        if sector:
            found_sector_count += 1
        rows.append({"ticker": ticker, "company_name": company_name or "", "sector": sector or "", "exchange": ""})
        print(f"  [{i + 1}/{len(tickers)}] {ticker}: company_name={company_name!r} sector={sector!r}")
        if i < len(tickers) - 1 and REQUEST_DELAY_SECONDS > 0:
            time.sleep(REQUEST_DELAY_SECONDS)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ticker", "company_name", "sector", "exchange"])
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nProcessed {len(tickers)} tickers, found a sector for {found_sector_count}/{len(tickers)}.")
    print(f"Wrote {output_path}")
    print("Review/correct it by hand (especially any blank sectors), then:")
    print(f"  python scripts/upload_manual_tickers.py --file {output_path}")


if __name__ == "__main__":
    main()
