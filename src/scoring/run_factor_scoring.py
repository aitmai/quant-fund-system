"""
Factor scoring orchestrator (DESIGN.md §3 Stage 2, §7 cron summary).

Ties together the bulk-fetch layer (data_fetch.py), the price-based
factors (momentum_lowvol.py), the fundamentals-based factors
(quality_value.py), and sector-relative z-scoring (zscore.py) into the
weekly job that populates `factor_scores`.

Design calls locked in 2026-07-13 (not specified in DESIGN.md itself):
  - Z-scoring is SECTOR-RELATIVE, not universe-wide.
  - Missing data EXCLUDES a ticker from that specific factor (NULL),
    never imputes a neutral/zero score.
  - Every run prints how many active tickers are missing each factor,
    against the total active universe — not buried in a warning, front
    and center in the summary every single run.

OPEN QUESTION, flagged rather than silently decided: `factor_scores.decile_rank`
is a single column, but DESIGN.md's fix #5 explicitly removed the
fixed-weight composite score this table was presumably designed around
(Stage 3's ML model does the weighting/ranking now, not Stage 2). What
"decile" would even mean here without a composite isn't specified
anywhere — left NULL for now rather than inventing a meaning. If you want
a specific per-factor decile (e.g. momentum decile, quality decile),
that's a schema change (4 columns, not 1) worth deciding explicitly.
"""

import sys
from datetime import date
from typing import Dict

from . import data_fetch
from .momentum_lowvol import compute_momentum_and_lowvol
from .quality_value import QUALITY_SUB_METRICS, VALUE_SUB_METRICS, compute_quality_value_raw
from .zscore import average_available_zscores, sector_zscore


def run(conn, job=None, score_date=None) -> dict:
    score_date = score_date or date.today()

    universe_df = data_fetch.fetch_active_universe(conn)
    total_active = len(universe_df)
    sector_map: Dict[str, str] = dict(zip(universe_df["ticker"], universe_df["sector"]))

    if total_active == 0:
        msg = "Factor scoring skipped: no active tickers in universe."
        print(msg, file=sys.stderr)
        if job:
            job.note(msg)
        return {"total_active": 0, "scored": 0}

    price_panel = data_fetch.fetch_price_panel(conn, as_of=score_date)
    fundamentals_panel = data_fetch.fetch_latest_fundamentals(conn, as_of=score_date)

    momentum_raw, lowvol_raw, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(price_panel)
    quality_sub_raw, value_sub_raw = compute_quality_value_raw(fundamentals_panel)

    momentum_z = sector_zscore(momentum_raw, sector_map)
    lowvol_z = sector_zscore(lowvol_raw, sector_map)

    quality_sub_z = {name: sector_zscore(quality_sub_raw[name], sector_map) for name in QUALITY_SUB_METRICS}
    value_sub_z = {name: sector_zscore(value_sub_raw[name], sector_map) for name in VALUE_SUB_METRICS}

    quality_z = average_available_zscores(list(quality_sub_z.values()))
    value_z = average_available_zscores(list(value_sub_z.values()))

    all_tickers = set(universe_df["ticker"])
    rows = []
    for ticker in all_tickers:
        rows.append(
            (
                ticker,
                score_date,
                momentum_z.get(ticker),
                quality_z.get(ticker),
                value_z.get(ticker),
                lowvol_z.get(ticker),
            )
        )

    run_type = job.run_type if job else "manual"
    run_id = job.run_id if job else f"factor_scoring-manual-{score_date.isoformat()}"
    _upsert_factor_scores(conn, rows, run_type, run_id)

    summary = _build_diagnostic_summary(
        total_active, momentum_z, lowvol_z, quality_z, value_z,
        momentum_skipped, lowvol_skipped,
    )
    _print_diagnostics(summary)
    if job:
        job.note(
            f"scored={summary['scored_at_least_one']}/{total_active} active tickers; "
            f"momentum={summary['momentum']['populated']}/{total_active}, "
            f"lowvol={summary['lowvol']['populated']}/{total_active}, "
            f"quality={summary['quality']['populated']}/{total_active}, "
            f"value={summary['value']['populated']}/{total_active}"
        )
    return summary


def _upsert_factor_scores(conn, rows, run_type: str, run_id: str):
    with conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO factor_scores
                    (ticker, score_date, momentum_z, quality_z, value_z, lowvol_z, run_type, run_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (ticker, score_date, run_id) DO UPDATE SET
                    momentum_z = EXCLUDED.momentum_z, quality_z = EXCLUDED.quality_z,
                    value_z = EXCLUDED.value_z, lowvol_z = EXCLUDED.lowvol_z
                """,
                [(t, d, m, q, v, lv, run_type, run_id) for (t, d, m, q, v, lv) in rows],
            )


def _build_diagnostic_summary(total_active, momentum_z, lowvol_z, quality_z, value_z, momentum_skipped, lowvol_skipped) -> dict:
    scored_at_least_one = len(
        set(momentum_z) | set(lowvol_z) | set(quality_z) | set(value_z)
    )
    return {
        "total_active": total_active,
        "scored_at_least_one": scored_at_least_one,
        "momentum": {
            "populated": len(momentum_z),
            "missing": total_active - len(momentum_z),
            "insufficient_history": len(momentum_skipped),
        },
        "lowvol": {
            "populated": len(lowvol_z),
            "missing": total_active - len(lowvol_z),
            "insufficient_history": len(lowvol_skipped),
        },
        "quality": {
            "populated": len(quality_z),
            "missing": total_active - len(quality_z),
        },
        "value": {
            "populated": len(value_z),
            "missing": total_active - len(value_z),
        },
    }


def _print_diagnostics(summary: dict):
    total = summary["total_active"]
    print(f"Factor scoring: {total} active tickers")
    for factor in ("momentum", "lowvol", "quality", "value"):
        s = summary[factor]
        extra = f" ({s['insufficient_history']} due to insufficient price history)" if "insufficient_history" in s else ""
        print(f"  {factor}: {s['populated']}/{total} scored, {s['missing']}/{total} missing{extra}")
