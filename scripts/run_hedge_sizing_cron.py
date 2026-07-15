"""
Daily hedge sleeve sizing — Phase 9, DESIGN.md §3.

Ties together every Phase 9 piece into one vol_hedge_state row per day:
  - SPY ~30-delta, 30-45 DTE put selection (options_provider.py)
  - VIX futures term structure signal (vix_futures_provider.py)
  - Realized volatility + GARCH(1,1) forecast on SPY (volatility.py)

KNOWN v1 SCOPE LIMITATIONS (decided 2026-07-15, revisit as noted):

1. Target dollar allocation uses HEDGE_SLEEVE_NAV_PLACEHOLDER (.env) x
   10%, NOT real portfolio NAV — fund_metadata/portfolio_snapshots don't
   exist until Phase 7 (Trade Tracking & Positions). Replace this
   sizing input once Phase 7 provides real NAV.

2. The hedge sleeve's target allocation is a FIXED 10% of NAV, not
   dynamically scaled by the vol signals. DESIGN.md's "Sizing/timing
   scale continuously rather than jumping in discrete steps" describes
   an intent, not a specified formula — no combination rule for implied-
   vs-realized spread + term structure + GARCH forecast into a sizing
   multiplier exists in DESIGN.md. Inventing one silently would bury a
   real modeling decision inside working-looking code; this is
   deliberately left as an explicit open item instead. The three
   signals ARE computed and logged into vol_hedge_state every run, so
   the data needed to design that formula later is already accumulating.

3. roll_cost_running and theta_decay_running are written as 0 every
   run, not accumulated day-over-day. Both are only meaningful against a
   REAL held position with a real entry price/date — which doesn't
   exist yet (this phase's own milestone is "a sensible contract count
   WITHOUT live capital behind it yet," per DESIGN.md §13 Phase 9).
   Wire these to real accumulation once Phase 7 positions exist.

4. hedge_action is a simple descriptive string ("sized" / "rolled_and_
   sized"), not a real position-lifecycle state machine (open/hold/
   close/roll) — same Phase 7 dependency as #3.

Usage:
    python scripts/run_hedge_sizing_cron.py
    python scripts/run_hedge_sizing_cron.py --triggered-by aitmai
"""

import argparse
import os
import sys
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.hedge.sizing import contracts_for_target_allocation
from src.hedge.spy_data import fetch_spy_price_series
from src.hedge.volatility import daily_returns_pct, garch_forecast_annualized, realized_volatility_annualized
from src.providers.options_provider import OptionsProviderError, select_spy_hedge_put
from src.providers.vix_futures_provider import (
    VixFuturesProviderError,
    fetch_effective_sizing_quote,
    fetch_term_structure,
)

HEDGE_SLEEVE_PCT = 0.10  # DESIGN.md §2/§3: fixed 10% of the dual-sleeve split


def target_dollar_allocation() -> float:
    nav_placeholder = float(os.environ.get("HEDGE_SLEEVE_NAV_PLACEHOLDER", "1000000"))
    return nav_placeholder * HEDGE_SLEEVE_PCT


def run(conn, as_of=None) -> dict:
    as_of = as_of or date.today()

    selected_put = select_spy_hedge_put(as_of=as_of)  # raises OptionsProviderError on failure — never a fake put
    term_structure = fetch_term_structure(as_of=as_of)  # pure calendar front/next, for the signal itself
    sizing_quote, rolled = fetch_effective_sizing_quote(as_of=as_of)  # roll-aware, for the price actually used

    spy_prices = fetch_spy_price_series(conn, as_of=as_of)
    spy_returns = daily_returns_pct(spy_prices)
    realized_vol = realized_volatility_annualized(spy_returns)
    garch_forecast = garch_forecast_annualized(spy_returns)

    target_dollars = target_dollar_allocation()
    contracts = contracts_for_target_allocation(target_dollars, premium=selected_put.premium)
    if contracts is None:
        contracts = 0
    actual_dollar_amount = contracts * 100 * selected_put.premium

    hedge_action = "rolled_and_sized" if rolled else "sized"

    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO vol_hedge_state (
                    date, vix_futures_contract_month, vix_futures_price, roll_cost_running,
                    implied_vol, realized_vol, garch_forecast, term_structure_signal,
                    spy_put_strike, spy_put_dte, spy_put_delta, spy_put_premium,
                    theta_decay_running, hedge_action, hedge_dollar_amount, contracts_held
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (date) DO UPDATE SET
                    vix_futures_contract_month = EXCLUDED.vix_futures_contract_month,
                    vix_futures_price = EXCLUDED.vix_futures_price,
                    roll_cost_running = EXCLUDED.roll_cost_running,
                    implied_vol = EXCLUDED.implied_vol,
                    realized_vol = EXCLUDED.realized_vol,
                    garch_forecast = EXCLUDED.garch_forecast,
                    term_structure_signal = EXCLUDED.term_structure_signal,
                    spy_put_strike = EXCLUDED.spy_put_strike,
                    spy_put_dte = EXCLUDED.spy_put_dte,
                    spy_put_delta = EXCLUDED.spy_put_delta,
                    spy_put_premium = EXCLUDED.spy_put_premium,
                    theta_decay_running = EXCLUDED.theta_decay_running,
                    hedge_action = EXCLUDED.hedge_action,
                    hedge_dollar_amount = EXCLUDED.hedge_dollar_amount,
                    contracts_held = EXCLUDED.contracts_held
                """,
                (
                    as_of, sizing_quote.contract_expiration.isoformat(), sizing_quote.settle, 0,
                    selected_put.implied_vol, realized_vol, garch_forecast,
                    term_structure.term_structure_signal,
                    selected_put.strike, selected_put.days_to_expiration, selected_put.delta,
                    selected_put.premium, 0, hedge_action, actual_dollar_amount, contracts,
                ),
            )

    return {
        "date": as_of,
        "spy_put_strike": selected_put.strike,
        "spy_put_dte": selected_put.days_to_expiration,
        "spy_put_delta": selected_put.delta,
        "spy_put_premium": selected_put.premium,
        "vix_contract": sizing_quote.contract_expiration,
        "vix_price": sizing_quote.settle,
        "vix_rolled": rolled,
        "term_structure_signal": term_structure.term_structure_signal,
        "realized_vol": realized_vol,
        "garch_forecast": garch_forecast,
        "target_dollars": target_dollars,
        "contracts_held": contracts,
        "hedge_dollar_amount": actual_dollar_amount,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily hedge sleeve sizing (Phase 9).")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()

    conn = get_connection()
    try:
        with JobRun(conn, stage="hedge_sizing", run_type="manual", triggered_by=args.triggered_by) as job:
            summary = run(conn)
            job.note(f"contracts_held={summary['contracts_held']} vix_rolled={summary['vix_rolled']}")
        print(summary)
        return 0
    except (OptionsProviderError, VixFuturesProviderError) as exc:
        print(f"ERROR: hedge sizing failed (data provider): {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: hedge sizing failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
