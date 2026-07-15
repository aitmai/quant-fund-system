"""
One-time fund setup — Phase 7, DESIGN.md §6.5 / §13 Phase 7.

Initializes the single-row fund_metadata table that record_trade.py and
compute_portfolio_snapshot.py both depend on for NAV/cash math. This is
the "fund_start_date" and "initial_capital" the trailing-returns screen
(DESIGN.md §11.2) measures everything against — get these right before
recording any real trades.

Idempotent-by-refusal, not idempotent-by-overwrite: running this twice
does NOT silently reset an already-initialized fund (that would quietly
corrupt the return history everything downstream depends on). Use
--force to intentionally reset during testing/paper-trading setup.

Usage:
    python scripts/init_fund_metadata.py --start-date 2026-07-15 --capital 1000000
    python scripts/init_fund_metadata.py --start-date 2026-07-15 --capital 1000000 --force
"""

import argparse
import sys
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection


def main():
    parser = argparse.ArgumentParser(description="Initialize fund_metadata (Phase 7).")
    parser.add_argument("--start-date", required=True, help="Fund inception date, YYYY-MM-DD")
    parser.add_argument("--capital", required=True, type=float, help="Initial capital, e.g. 1000000")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an already-initialized fund_metadata row. Use with caution — "
             "this resets the anchor every trailing-return calculation depends on.",
    )
    args = parser.parse_args()

    try:
        start_date = date.fromisoformat(args.start_date)
    except ValueError:
        print(f"ERROR: --start-date must be YYYY-MM-DD, got {args.start_date!r}", file=sys.stderr)
        sys.exit(1)

    if args.capital <= 0:
        print(f"ERROR: --capital must be positive, got {args.capital}", file=sys.stderr)
        sys.exit(1)

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fund_start_date, initial_capital FROM fund_metadata WHERE id = 1")
            existing = cur.fetchone()

        if existing and not args.force:
            print(
                f"fund_metadata is already initialized: "
                f"fund_start_date={existing[0]}, initial_capital={existing[1]}\n"
                f"Refusing to overwrite. Re-run with --force if this is intentional.",
                file=sys.stderr,
            )
            sys.exit(1)

        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO fund_metadata (id, fund_start_date, initial_capital, total_cash_invested, last_updated)
                    VALUES (1, %s, %s, %s, NOW())
                    ON CONFLICT (id) DO UPDATE
                        SET fund_start_date = EXCLUDED.fund_start_date,
                            initial_capital = EXCLUDED.initial_capital,
                            total_cash_invested = EXCLUDED.total_cash_invested,
                            last_updated = NOW()
                    """,
                    (start_date, args.capital, args.capital),
                )
        print(f"fund_metadata initialized: fund_start_date={start_date}, initial_capital={args.capital:,.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
