"""
Manual ticker upload — DESIGN.md §3 Stage 1 ("plus optional manually
uploaded ticker lists") and §6.1 (universe.source = 'manual').

CSV format (header row required): ticker,company_name,sector,exchange
Only `ticker` is required; the rest are optional.

Usage:
    python scripts/upload_manual_tickers.py --file my_tickers.csv --triggered-by aitmai
"""

import argparse
import csv
import sys

sys.path.insert(0, ".")

# Must run BEFORE any `src.*` import below — see run_ingestion_cron.py
# for why placement matters (module-level env reads in provider files).
from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.universe.construct_universe import upload_manual_tickers


def read_csv(path: str):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("ticker"):
                # Blank CSV cells come back as "" from csv.DictReader, not
                # None — left as-is, that would flow into
                # upload_manual_tickers()'s COALESCE(EXCLUDED.x, universe.x)
                # as a non-null empty string, silently OVERWRITING any
                # existing good company_name/sector/exchange with blank.
                # Converting "" -> None here lets COALESCE actually do its
                # job: preserve existing data when a column is blank.
                yield {k: (v if v else None) for k, v in row.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a manual ticker list into the universe table.")
    parser.add_argument("--file", required=True, help="Path to CSV with a 'ticker' column (company_name, sector, exchange optional)")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()

    conn = get_connection()
    try:
        with JobRun(conn, stage="universe_manual_upload", run_type="manual", triggered_by=args.triggered_by) as job:
            rows = list(read_csv(args.file))
            if not rows:
                print(f"No valid rows found in {args.file} (need a 'ticker' column).", file=sys.stderr)
                return 1
            result = upload_manual_tickers(conn, rows, triggered_by=args.triggered_by)
            print(f"Uploaded {result['added']} tickers from {args.file}")
            job.note(f"file={args.file} added={result['added']}")
        return 0
    except FileNotFoundError:
        print(f"ERROR: file not found: {args.file}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: manual upload failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
