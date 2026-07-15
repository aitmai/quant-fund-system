"""
Compute one day's portfolio_snapshots row — Phase 7, DESIGN.md §6.5 / §13
Phase 7.

Run this once per trading day, after that day's trades (if any) have been
recorded via record_trade.py. It's the step that turns the raw trades/
positions ledger into the single daily NAV number everything else (the
Dashboard, the Returns screen's trailing-horizon calculations) reads.

What it does:
  1. Re-marks the LONG sleeve's positions to the latest available
     price_history close on or before the snapshot date, updating
     positions.current_price/market_value/unrealized_pnl in place.
  2. Computes cash_balance by replaying every trade's cash impact against
     fund_metadata.initial_capital (buys subtract dollar_amount +
     commission, sells add dollar_amount - commission) — this is the
     literal ledger, not a running/cached balance, so it's always
     derivable from source-of-truth trades even if this script has never
     run before.
  3. Sums long_sleeve_value + hedge_sleeve_value + cash_balance = NAV,
     computes daily_pnl/daily_return_pct against the prior snapshot (or
     against initial_capital if this is the first one), and cumulative
     return since fund_start_date.
  4. Upserts the result into portfolio_snapshots for the given date.

KNOWN v1 LIMITATIONS (same explicit-open-item pattern as Phase 9's and
record_trade.py's addenda):
  - Only the LONG sleeve gets re-marked to a live price here. The HEDGE
    sleeve (VIX futures / SPY puts) has no wired-up daily settlement/
    premium re-marking path yet — those positions' current_price stays
    at whatever record_trade.py last set it to (the execution price of
    the last trade in that position), so hedge_sleeve_value can go stale
    between hedge trades. Wire this to vix_futures_provider.py /
    options_provider.py once the hedge sleeve is actually holding a
    Phase 9 position (see run_hedge_sizing_cron.py's own limitations).
  - If a long-sleeve ticker has no price_history row on or before the
    snapshot date (e.g. ingestion hasn't caught up yet for that day),
    that position's market_value falls back to its last-known
    current_price rather than blocking the whole snapshot — logged as a
    JobRun note so it's visible, not silent.

Usage:
    python scripts/compute_portfolio_snapshot.py
    python scripts/compute_portfolio_snapshot.py --date 2026-07-15
"""

import argparse
import sys
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun


def _mark_long_sleeve_to_market(cur, snapshot_date, job):
    cur.execute("SELECT ticker, quantity, avg_cost_basis, current_price FROM positions WHERE sleeve = 'long'")
    long_positions = cur.fetchall()

    for ticker, quantity, avg_cost_basis, stale_price in long_positions:
        cur.execute(
            """
            SELECT close FROM price_history
            WHERE ticker = %s AND date <= %s
            ORDER BY date DESC LIMIT 1
            """,
            (ticker, snapshot_date),
        )
        row = cur.fetchone()
        if row and row[0] is not None:
            mark_price = float(row[0])
        else:
            mark_price = float(stale_price) if stale_price is not None else float(avg_cost_basis)
            job.note(f"no price_history for {ticker} on/before {snapshot_date}, used stale price {mark_price}")

        market_value = float(quantity) * mark_price
        unrealized_pnl = market_value - (float(quantity) * float(avg_cost_basis))
        cur.execute(
            """
            UPDATE positions
            SET current_price = %s, market_value = %s, unrealized_pnl = %s, last_updated = NOW()
            WHERE ticker = %s AND sleeve = 'long'
            """,
            (mark_price, market_value, unrealized_pnl, ticker),
        )


def _compute_cash_balance(cur, initial_capital):
    cur.execute(
        """
        SELECT COALESCE(SUM(
            CASE WHEN side = 'buy' THEN -(dollar_amount + commission_fees)
                 ELSE (dollar_amount - commission_fees)
            END
        ), 0)
        FROM trades
        """
    )
    net_cash_flow = cur.fetchone()[0]
    return float(initial_capital) + float(net_cash_flow)


def main():
    parser = argparse.ArgumentParser(description="Compute a daily portfolio_snapshots row (Phase 7).")
    parser.add_argument("--date", help="Snapshot date, YYYY-MM-DD, defaults to today")
    args = parser.parse_args()
    snapshot_date = date.fromisoformat(args.date) if args.date else date.today()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT fund_start_date, initial_capital FROM fund_metadata WHERE id = 1")
            fund_row = cur.fetchone()
        if not fund_row or not fund_row[0]:
            print(
                "ERROR: fund_metadata is not initialized. Run scripts/init_fund_metadata.py first.",
                file=sys.stderr,
            )
            sys.exit(1)
        fund_start_date, initial_capital = fund_row

        if snapshot_date < fund_start_date:
            print(
                f"ERROR: snapshot date {snapshot_date} is before fund_start_date {fund_start_date}.",
                file=sys.stderr,
            )
            sys.exit(1)

        with JobRun(conn, stage="portfolio_snapshot", run_type="manual") as job:
            with conn:
                with conn.cursor() as cur:
                    _mark_long_sleeve_to_market(cur, snapshot_date, job)

                    cash_balance = _compute_cash_balance(cur, initial_capital)

                    cur.execute("SELECT COALESCE(SUM(market_value), 0) FROM positions WHERE sleeve = 'long'")
                    long_sleeve_value = float(cur.fetchone()[0])

                    cur.execute("SELECT COALESCE(SUM(market_value), 0) FROM positions WHERE sleeve = 'hedge'")
                    hedge_sleeve_value = float(cur.fetchone()[0])

                    total_nav = cash_balance + long_sleeve_value + hedge_sleeve_value

                    cur.execute(
                        "SELECT total_nav FROM portfolio_snapshots WHERE snapshot_date < %s ORDER BY snapshot_date DESC LIMIT 1",
                        (snapshot_date,),
                    )
                    prev_row = cur.fetchone()
                    prev_nav = float(prev_row[0]) if prev_row else float(initial_capital)

                    daily_pnl = total_nav - prev_nav
                    daily_return_pct = (daily_pnl / prev_nav * 100) if prev_nav else None
                    cumulative_return_pct = (
                        (total_nav - float(initial_capital)) / float(initial_capital) * 100
                    )

                    cur.execute(
                        """
                        INSERT INTO portfolio_snapshots
                            (snapshot_date, total_nav, long_sleeve_value, hedge_sleeve_value,
                             cash_balance, daily_pnl, daily_return_pct, cumulative_return_pct)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (snapshot_date) DO UPDATE
                            SET total_nav = EXCLUDED.total_nav,
                                long_sleeve_value = EXCLUDED.long_sleeve_value,
                                hedge_sleeve_value = EXCLUDED.hedge_sleeve_value,
                                cash_balance = EXCLUDED.cash_balance,
                                daily_pnl = EXCLUDED.daily_pnl,
                                daily_return_pct = EXCLUDED.daily_return_pct,
                                cumulative_return_pct = EXCLUDED.cumulative_return_pct
                        """,
                        (snapshot_date, total_nav, long_sleeve_value, hedge_sleeve_value,
                         cash_balance, daily_pnl, daily_return_pct, cumulative_return_pct),
                    )
                    job.note(f"NAV={total_nav:.2f} cash={cash_balance:.2f} long={long_sleeve_value:.2f} hedge={hedge_sleeve_value:.2f}")

            print(
                f"Snapshot {snapshot_date}: NAV={total_nav:,.2f}  "
                f"daily_pnl={daily_pnl:,.2f} ({daily_return_pct:.3f}%)  "
                f"cumulative={cumulative_return_pct:.3f}%"
            )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
