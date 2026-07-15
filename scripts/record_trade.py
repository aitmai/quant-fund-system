"""
Record a manually-confirmed trade — Phase 7, DESIGN.md §6.5 / §13 Phase 7.

This is the "signal vs. execution" boundary DESIGN.md describes: a
daily_picks row is a SIGNAL, this script is what turns it into an
EXECUTED trade once you've actually placed (or manually confirmed) it
with your broker. Nothing auto-trades at this stage — you run this by
hand, once per fill, after the trade has actually happened.

What it does, per trade:
  1. Inserts one row into `trades` (the permanent ledger — never edited
     after the fact; a mistake gets corrected with an offsetting trade,
     not a row edit, so the ledger stays an honest record of what was
     actually done).
  2. Updates `positions` (the current-state view derived FROM the
     ledger): a buy adds to quantity and recomputes a weighted-average
     cost basis; a sell reduces quantity and, on the way to zero, writes
     the realized gain/loss to `realized_pnl` and closes the position
     out of `positions` entirely (not zeroed rows).

KNOWN v1 LIMITATIONS (same pattern as Phase 9's addendum — explicit
open items, not silent gaps):
  - No short selling: a sell is rejected if it exceeds the currently
    held quantity for that (ticker, sleeve). Only long positions exist
    in this table.
  - `positions.current_price` is set to the trade's execution price at
    the moment of the trade, not continuously marked-to-market. Between
    trades a position's `current_price`/`market_value`/`unrealized_pnl`
    go stale until either another trade happens or a separate mark-to-
    market step is built (compute_portfolio_snapshot.py currently
    re-marks the LONG sleeve from price_history at snapshot time; the
    HEDGE sleeve has no live re-marking path yet — see that script's
    own limitations note).
  - On a partial sell, `avg_cost_basis` and `purchase_date` are left
    unchanged (standard average-cost-basis accounting, not FIFO/LIFO
    lot tracking) — DESIGN.md doesn't specify a lot-accounting method,
    so average-cost was chosen as the simpler, more common default;
    revisit if a specific method becomes a stated requirement.

Usage:
    python scripts/record_trade.py --ticker AAPL --side buy --sleeve long \\
        --instrument-type equity --quantity 10 --price 225.40

    python scripts/record_trade.py --ticker AAPL --side sell --sleeve long \\
        --instrument-type equity --quantity 4 --price 231.10 --notes "partial exit"
"""

import argparse
import sys
import uuid
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun


def _get_fund_start_date(cur):
    cur.execute("SELECT fund_start_date FROM fund_metadata WHERE id = 1")
    row = cur.fetchone()
    if not row or not row[0]:
        print(
            "ERROR: fund_metadata is not initialized. Run scripts/init_fund_metadata.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return row[0]


def _get_position(cur, ticker: str, sleeve: str):
    cur.execute(
        "SELECT quantity, avg_cost_basis, purchase_date FROM positions WHERE ticker = %s AND sleeve = %s",
        (ticker, sleeve),
    )
    row = cur.fetchone()
    if row is None:
        return None
    # psycopg2 returns NUMERIC columns as decimal.Decimal, not float — cast
    # here, once, so every caller downstream can freely mix these with the
    # plain floats coming from argparse (--price, --quantity) without
    # hitting "unsupported operand type(s) for -: 'Decimal' and 'float'".
    quantity, avg_cost_basis, purchase_date = row
    return (float(quantity), float(avg_cost_basis), purchase_date)


def _record_buy(cur, ticker, sleeve, quantity, price, trade_date):
    existing = _get_position(cur, ticker, sleeve)
    if existing:
        old_qty, old_avg_cost, purchase_date = existing
        new_qty = old_qty + quantity
        new_avg_cost = ((old_qty * old_avg_cost) + (quantity * price)) / new_qty
    else:
        new_qty = quantity
        new_avg_cost = price
        purchase_date = trade_date

    market_value = new_qty * price
    unrealized_pnl = market_value - (new_qty * new_avg_cost)

    cur.execute(
        """
        INSERT INTO positions (ticker, sleeve, quantity, avg_cost_basis, current_price,
                                market_value, unrealized_pnl, purchase_date, last_updated)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (ticker, sleeve) DO UPDATE
            SET quantity = EXCLUDED.quantity,
                avg_cost_basis = EXCLUDED.avg_cost_basis,
                current_price = EXCLUDED.current_price,
                market_value = EXCLUDED.market_value,
                unrealized_pnl = EXCLUDED.unrealized_pnl,
                last_updated = NOW()
        """,
        (ticker, sleeve, new_qty, new_avg_cost, price, market_value, unrealized_pnl, purchase_date),
    )


def _record_sell(cur, ticker, sleeve, quantity, price, trade_date, commission_fees):
    existing = _get_position(cur, ticker, sleeve)
    if not existing or existing[0] < quantity:
        held = existing[0] if existing else 0
        print(
            f"ERROR: cannot sell {quantity} {ticker} ({sleeve}) — only {held} held. "
            f"Short selling is not supported.",
            file=sys.stderr,
        )
        sys.exit(1)

    old_qty, avg_cost_basis, purchase_date = existing
    remaining_qty = old_qty - quantity

    proceeds = quantity * price
    cost_basis = quantity * avg_cost_basis
    realized_gain_loss = proceeds - cost_basis - commission_fees
    return_pct = (realized_gain_loss / cost_basis * 100) if cost_basis else None
    holding_period_days = (trade_date - purchase_date).days if purchase_date else None

    cur.execute(
        """
        INSERT INTO realized_pnl (ticker, sleeve, open_date, close_date, holding_period_days,
                                   cost_basis, proceeds, realized_gain_loss, return_pct, exit_reason)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (ticker, sleeve, purchase_date, trade_date, holding_period_days,
         cost_basis, proceeds, realized_gain_loss, return_pct, "manual_trade"),
    )

    if remaining_qty == 0:
        cur.execute("DELETE FROM positions WHERE ticker = %s AND sleeve = %s", (ticker, sleeve))
    else:
        market_value = remaining_qty * price
        unrealized_pnl = market_value - (remaining_qty * avg_cost_basis)
        cur.execute(
            """
            UPDATE positions
            SET quantity = %s, current_price = %s, market_value = %s,
                unrealized_pnl = %s, last_updated = NOW()
            WHERE ticker = %s AND sleeve = %s
            """,
            (remaining_qty, price, market_value, unrealized_pnl, ticker, sleeve),
        )

    return realized_gain_loss


def main():
    parser = argparse.ArgumentParser(description="Record a manually-confirmed trade (Phase 7).")
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--side", required=True, choices=["buy", "sell"])
    parser.add_argument("--sleeve", required=True, choices=["long", "hedge"])
    parser.add_argument("--instrument-type", required=True, choices=["equity", "vix_future", "spy_put"])
    parser.add_argument("--quantity", required=True, type=float)
    parser.add_argument("--price", required=True, type=float)
    parser.add_argument("--commission", type=float, default=0.0)
    parser.add_argument("--trade-date", help="YYYY-MM-DD, defaults to today")
    parser.add_argument("--notes", default=None)
    args = parser.parse_args()

    ticker = args.ticker.upper()
    trade_date = date.fromisoformat(args.trade_date) if args.trade_date else date.today()
    dollar_amount = args.quantity * args.price

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            _get_fund_start_date(cur)  # fails fast with a clear message if fund isn't set up yet

        with JobRun(conn, stage="record_trade", run_type="manual") as job:
            trade_id = f"trade-{uuid.uuid4().hex[:12]}"
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO trades (trade_id, trade_date, ticker, side, sleeve, instrument_type,
                                             quantity, price, dollar_amount, commission_fees, run_id, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (trade_id, trade_date, ticker, args.side, args.sleeve, args.instrument_type,
                         args.quantity, args.price, dollar_amount, args.commission, job.run_id, args.notes),
                    )

                    if args.side == "buy":
                        _record_buy(cur, ticker, args.sleeve, args.quantity, args.price, trade_date)
                        job.note(f"bought {args.quantity} {ticker} ({args.sleeve}) @ {args.price}")
                    else:
                        realized = _record_sell(
                            cur, ticker, args.sleeve, args.quantity, args.price, trade_date, args.commission
                        )
                        job.note(f"sold {args.quantity} {ticker} ({args.sleeve}) @ {args.price}, realized {realized:.2f}")

            print(f"Recorded {trade_id}: {args.side} {args.quantity} {ticker} ({args.sleeve}) @ {args.price:.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
