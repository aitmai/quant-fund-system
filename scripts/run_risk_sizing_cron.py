"""
Daily Stage 5 — Risk-Parity Position Sizing — DESIGN.md §3 Stage 5,
§13 Phase 5.

Final stage of the daily-picks pipeline (Stage 2 factor scoring -> Stage
3 ML ranking -> Stage 4 correlation/sector filter -> THIS). Takes Stage
4's accepted shortlist, sizes each candidate by risk-parity, and writes
the top DAILY_PICKS_COUNT by that sizing to `daily_picks` — this is the
table the GUI's Positions tab actually reads "tomorrow's picks" from.

Formula (DESIGN.md §3 Stage 5):
    Position_size = Base_$1000 x (Target_vol / Stock_realized_vol)
Higher realized vol -> smaller dollar allocation (same target risk
contribution from every position, hence "risk-parity" — a volatile
stock gets a smaller dollar bet so its RISK contribution matches a calm
stock's larger dollar bet).

Realized vol reuses src/hedge/volatility.py's daily_returns_pct() +
realized_volatility_annualized() unchanged — same trailing-21-day
annualized-decimal calculation already built and verified for the hedge
sleeve, applied here to individual equities instead of SPY. Reusing it
rather than reimplementing keeps exactly one volatility convention in
this codebase, not two subtly different ones.

KNOWN v1 LIMITATIONS (explicit open items, same pattern as this repo's
other phase addenda):
  - `next_trading_day()` only skips weekends, not market holidays —
    DESIGN.md doesn't specify a holiday calendar and this repo has no
    trading-calendar dependency yet (e.g. pandas_market_calendars). A
    pick generated the evening before a market holiday would get a
    trade_date that's actually closed. Low-frequency edge case (a
    handful of days/year), worth fixing once a holiday calendar is
    actually wired in somewhere in this codebase rather than adding a
    hardcoded holiday list here.
  - `position_size` and `dollar_allocated` are set to the SAME computed
    value. DESIGN.md's schema has both columns but only specifies one
    formula ("Position_size = ..."), with no stated distinction or
    further transformation (rounding, capping) between "the computed
    size" and "the actually allocated dollars" — until such a rule is
    specified, duplicating the one number into both columns is more
    honest than inventing an unstated adjustment.
  - A candidate Stage 4 accepted but with insufficient price_history to
    compute a realized vol (fewer than 21 trading days) is excluded
    here rather than sized with a fabricated number — logged via
    job.note(), and the next-ranked accepted candidate takes its spot
    in the top DAILY_PICKS_COUNT.

Usage:
    python scripts/run_risk_sizing_cron.py
    python scripts/run_risk_sizing_cron.py --score-date 2026-07-15
"""

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.hedge.volatility import daily_returns_pct, realized_volatility_annualized
from src.job_run import JobRun

BASE_DOLLAR_AMOUNT = float(os.environ.get("RISK_SIZING_BASE_DOLLAR_AMOUNT", "1000"))
TARGET_VOLATILITY = float(os.environ.get("RISK_SIZING_TARGET_VOLATILITY", "0.15"))  # DESIGN.md default, ~S&P 500 long-run vol
REALIZED_VOL_WINDOW_DAYS = 21  # matches src/hedge/volatility.py's own default (~1 trading month)
DAILY_PICKS_COUNT = int(os.environ.get("DAILY_PICKS_COUNT", "10"))
PRICE_HISTORY_LOOKBACK_DAYS = 60  # calendar-day buffer so 21 TRADING days of history is available even across weekends


def _next_trading_day(as_of: date) -> date:
    """Next weekday after as_of. Does NOT account for market holidays —
    see module docstring's known limitations."""
    next_day = as_of + timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=Saturday, 6=Sunday
        next_day += timedelta(days=1)
    return next_day


def _fetch_stage4_accepted(cur, score_date):
    # Scoped to the SINGLE most recent completed correlation_filter run
    # for this score_date. Without this, a live run (2026-07-15) returned
    # 208 "accepted" candidates instead of 16 — correlation_filtered_shortlist
    # has no run-level dedup, so every rerun of Stage 4 that day (several,
    # while debugging the sector-cap fix) accumulated a fresh set of rows
    # rather than replacing the previous run's, and the naive
    # WHERE score_date = %s AND final_rank IS NOT NULL merged all of them
    # together — including SNDK/MRVL appearing 5x each, once per historical
    # run that had accepted them at rank 1/2.
    cur.execute(
        """
        WITH latest_run AS (
            SELECT r.run_id
            FROM correlation_filtered_shortlist r
            JOIN job_runs j ON j.run_id = r.run_id
            WHERE r.score_date = %s
            ORDER BY j.start_time DESC
            LIMIT 1
        )
        SELECT ticker, final_rank FROM correlation_filtered_shortlist
        WHERE score_date = %s AND final_rank IS NOT NULL
              AND run_id = (SELECT run_id FROM latest_run)
        ORDER BY final_rank
        """,
        (score_date, score_date),
    )
    return cur.fetchall()


def _fetch_realized_vol(cur, ticker, as_of_date):
    cur.execute(
        """
        SELECT close FROM price_history
        WHERE ticker = %s AND date <= %s
        ORDER BY date DESC
        LIMIT %s
        """,
        (ticker, as_of_date, REALIZED_VOL_WINDOW_DAYS + 5),  # small buffer for pct_change's leading NaN
    )
    rows = cur.fetchall()
    if len(rows) < REALIZED_VOL_WINDOW_DAYS:
        return None
    closes = [float(r[0]) for r in reversed(rows)]  # reversed: oldest-first, matching pd.Series expected order
    import pandas as pd
    prices = pd.Series(closes)
    returns_pct = daily_returns_pct(prices)
    return realized_volatility_annualized(returns_pct, window=REALIZED_VOL_WINDOW_DAYS)


def main():
    parser = argparse.ArgumentParser(description="Stage 5 risk-parity sizing (Phase 5).")
    parser.add_argument("--score-date", help="Size candidates filtered as of this date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()
    score_date = date.fromisoformat(args.score_date) if args.score_date else date.today()
    trade_date = _next_trading_day(score_date)

    conn = get_connection()
    try:
        with JobRun(conn, stage="risk_sizing", run_type="cron", triggered_by=args.triggered_by) as job:
            with conn.cursor() as cur:
                accepted = _fetch_stage4_accepted(cur, score_date)
                if not accepted:
                    print(f"ERROR: no accepted correlation_filtered_shortlist rows for {score_date}. Run Stage 4 first.", file=sys.stderr)
                    sys.exit(1)

                sized = []
                for ticker, final_rank in accepted:
                    realized_vol = _fetch_realized_vol(cur, ticker, score_date)
                    if realized_vol is None or realized_vol <= 0:
                        job.note(f"{ticker}: insufficient price history for realized vol, skipped")
                        continue
                    position_size = BASE_DOLLAR_AMOUNT * (TARGET_VOLATILITY / realized_vol)
                    sized.append((ticker, final_rank, realized_vol, position_size))
                    if len(sized) == DAILY_PICKS_COUNT:
                        break  # already have enough successfully-sized picks, no need to size the rest of the shortlist

            if not sized:
                print("ERROR: no candidates could be sized (no realized_vol computable for any of them).", file=sys.stderr)
                sys.exit(1)

            with conn:
                with conn.cursor() as cur:
                    for ticker, final_rank, realized_vol, position_size in sized:
                        cur.execute(
                            """
                            INSERT INTO daily_picks
                                (ticker, trade_date, position_size, target_vol, realized_vol,
                                 dollar_allocated, run_type, run_id, executed)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, FALSE)
                            ON CONFLICT (ticker, trade_date, run_id) DO UPDATE
                                SET position_size = EXCLUDED.position_size,
                                    target_vol = EXCLUDED.target_vol,
                                    realized_vol = EXCLUDED.realized_vol,
                                    dollar_allocated = EXCLUDED.dollar_allocated
                            """,
                            (ticker, trade_date, position_size, TARGET_VOLATILITY, realized_vol,
                             position_size, "cron", job.run_id),
                        )

            job.note(f"sized {len(sized)}/{len(accepted)} accepted candidates for trade_date {trade_date}")
            print(f"Sized {len(sized)}/{len(accepted)} accepted candidates for trade_date {trade_date}:")
            for ticker, final_rank, realized_vol, position_size in sized:
                print(f"  {final_rank:2d}. {ticker:6s}  realized_vol={realized_vol:.3f}  ${position_size:,.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
