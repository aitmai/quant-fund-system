"""
Daily Stage 4 — Correlation + Sector-Cap Filter — DESIGN.md §3 Stage 4,
§13 Phase 4.

Takes Stage 3's ranked shortlist and drops any candidate that's either
too correlated with something already accepted (a candidate ranked
higher this run, OR an existing held long-sleeve position) or would
push its GICS sector above the configured cap of total NAV.

Processes candidates in P(outperform) rank order, greedily: the
best-ranked candidate is checked first, and once accepted becomes part
of what subsequent, lower-ranked candidates are checked against — so a
later near-duplicate of an already-accepted name gets excluded, not the
other way around.

KNOWN v1 LIMITATIONS (explicit open items, same pattern as this repo's
other phase addenda):
  - **Sector cap is count-based, not dollar-based, and this is a real
    deviation from DESIGN.md's literal wording** ("reject a candidate
    if adding it would push that GICS sector above a configured
    ceiling of total NAV"). Found via a live run on 2026-07-15: a
    dollar-based check needs a real per-candidate dollar size, and
    Stage 5 (which computes that) hasn't run yet at this point in the
    pipeline — approximating with a flat placeholder dollar amount
    turned out to be mathematically toothless at this fund's NAV (even
    50 candidates at $1,000 each is only 5% of a ~$1M NAV, nowhere near
    a 15% cap, so the check could never fire). Instead: no more than
    TARGET_SHORTLIST_SIZE * SECTOR_CAP_PCT names in the ACCEPTED
    shortlist may share a sector — same 15% figure, applied to name-
    count instead of dollars, which directly bounds "one sector
    dominates the list" without depending on a Stage 5 number that
    doesn't exist yet. Revisit once Stage 5 is live: a two-pass design
    (rough-size candidates, THEN apply the real dollar-based cap) would
    match DESIGN.md's literal spec more closely.
  - A candidate with fewer than MIN_CORRELATION_HISTORY_DAYS of
    price_history is let through with correlation_flag=False rather
    than excluded — logged via job.note(), not silently skipped. A
    newly-listed or thinly-traded ticker shouldn't be penalized for
    something Stage 1/2 already let through; the sector cap still
    applies to it regardless.
  - "Currently held" for the correlation check means long-sleeve
    positions only (per DESIGN.md's own Stage 4 input: "Stage 3
    shortlist plus all currently held positions") — hedge-sleeve
    instruments (VIX futures, SPY puts) aren't equities and don't
    belong in an equity return-correlation matrix.

Usage:
    python scripts/run_correlation_filter_cron.py
    python scripts/run_correlation_filter_cron.py --score-date 2026-07-15
"""

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

import numpy as np
import pandas as pd

from src.db import get_connection
from src.job_run import JobRun

STAGE3_SHORTLIST_SIZE = int(os.environ.get("STAGE3_SHORTLIST_SIZE", "50"))
CORRELATION_WINDOW_DAYS = int(os.environ.get("CORRELATION_WINDOW_DAYS", "60"))
CORRELATION_EXCLUSION_THRESHOLD = float(os.environ.get("CORRELATION_EXCLUSION_THRESHOLD", "0.85"))
SECTOR_CAP_PCT = float(os.environ.get("SECTOR_CAP_PCT", "0.15"))
TARGET_SHORTLIST_SIZE = int(os.environ.get("TARGET_SHORTLIST_SIZE", "20"))  # DESIGN.md's stated Stage 4 output target (~10-20 names)
MAX_NAMES_PER_SECTOR = max(1, round(TARGET_SHORTLIST_SIZE * SECTOR_CAP_PCT))
MIN_CORRELATION_HISTORY_DAYS = 30  # below this, correlation isn't computed, sector cap still applies


def _fetch_stage3_shortlist(cur, score_date, limit):
    # Scoped to the SINGLE most recent completed ml_ranking run for this
    # score_date — NOT merged across every historical rerun. Discovered
    # via a live run (2026-07-15): ml_rankings/correlation_filtered_shortlist
    # have no run-level dedup, so re-running Stage 3 or Stage 4 multiple
    # times on the same day (as happens routinely while debugging, or
    # after a retry) accumulates a NEW set of rows per run rather than
    # replacing the previous run's rows. The old DISTINCT ON query below
    # merged every historical run's rows together — harmless for Stage 3
    # itself (a deterministic model scores the same ticker identically
    # across reruns, so DISTINCT ON's "best score" pick was a no-op in
    # practice), but the same pattern in Stage 5's read of THIS stage's
    # output was actively wrong (see run_risk_sizing_cron.py's fix).
    # Scoping here too for consistency, even though it wasn't visibly
    # broken — a future non-deterministic model change would silently
    # reintroduce the same class of bug otherwise.
    cur.execute(
        """
        WITH latest_run AS (
            SELECT r.run_id
            FROM ml_rankings r
            JOIN job_runs j ON j.run_id = r.run_id
            WHERE r.score_date = %s
            ORDER BY j.start_time DESC
            LIMIT 1
        )
        SELECT ticker, p_outperform
        FROM ml_rankings
        WHERE score_date = %s AND run_id = (SELECT run_id FROM latest_run)
        ORDER BY p_outperform DESC
        """,
        (score_date, score_date),
    )
    rows = cur.fetchall()
    return rows[:limit]


def _fetch_held_long_positions(cur):
    cur.execute("SELECT ticker, market_value FROM positions WHERE sleeve = 'long'")
    return cur.fetchall()


def _fetch_sectors(cur, tickers):
    if not tickers:
        return {}
    cur.execute("SELECT ticker, sector FROM universe WHERE ticker = ANY(%s)", (list(tickers),))
    return dict(cur.fetchall())


def _fetch_returns_matrix(cur, tickers, as_of_date, window_days):
    """Wide DataFrame: index=date, columns=ticker, values=daily pct return,
    over the trailing window_days trading days ending on/before as_of_date.
    Tickers with too little history end up as all-NaN columns — callers
    check coverage before trusting a given ticker's correlations."""
    if not tickers:
        return pd.DataFrame()
    lookback_start = as_of_date - timedelta(days=int(window_days * 1.6))  # generous calendar-day buffer for weekends/holidays
    cur.execute(
        """
        SELECT ticker, date, close FROM price_history
        WHERE ticker = ANY(%s) AND date <= %s AND date >= %s
        ORDER BY date
        """,
        (list(tickers), as_of_date, lookback_start),
    )
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ticker", "date", "close"])
    df["close"] = df["close"].astype(float)
    wide = df.pivot(index="date", columns="ticker", values="close").sort_index()
    wide = wide.tail(window_days + 1)  # +1 so pct_change() yields window_days return observations
    return wide.pct_change(fill_method=None).dropna(how="all")


def main():
    parser = argparse.ArgumentParser(description="Stage 4 correlation + sector-cap filter (Phase 4).")
    parser.add_argument("--score-date", help="Filter as of this date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()
    score_date = date.fromisoformat(args.score_date) if args.score_date else date.today()

    conn = get_connection()
    try:
        with JobRun(conn, stage="correlation_filter", run_type="cron", triggered_by=args.triggered_by) as job:
            with conn.cursor() as cur:
                candidates = _fetch_stage3_shortlist(cur, score_date, STAGE3_SHORTLIST_SIZE)
                if not candidates:
                    print(f"ERROR: no ml_rankings rows found for {score_date}. Run Stage 3 first.", file=sys.stderr)
                    sys.exit(1)

                held = _fetch_held_long_positions(cur)
                held_tickers = [t for t, _ in held]

                candidate_tickers = [t for t, _ in candidates]
                all_tickers = list(set(candidate_tickers) | set(held_tickers))

                sectors = _fetch_sectors(cur, all_tickers)
                returns = _fetch_returns_matrix(cur, all_tickers, score_date, CORRELATION_WINDOW_DAYS)

            corr_matrix = returns.corr() if not returns.empty else pd.DataFrame()

            # Running sector COUNT starts from what's actually held today —
            # a sector already concentrated in the real book should count
            # against new candidates from the same sector, same as the
            # dollar-based version DESIGN.md describes would.
            sector_counts = {}
            for t in held_tickers:
                sector = sectors.get(t)
                if sector:
                    sector_counts[sector] = sector_counts.get(sector, 0) + 1

            accepted = []
            results = []  # (ticker, correlation_flag, sector_cap_flag, excluded_due_to, final_rank)

            for ticker, p_outperform in candidates:
                excluded_due_to = None
                correlation_flag = False
                sector_cap_flag = False

                compare_against = accepted + held_tickers
                has_history = ticker in returns.columns and returns[ticker].notna().sum() >= MIN_CORRELATION_HISTORY_DAYS - 1
                if has_history and compare_against:
                    correlations = corr_matrix.loc[ticker, [t for t in compare_against if t in corr_matrix.columns]].abs()
                    if not correlations.empty and correlations.max() > CORRELATION_EXCLUSION_THRESHOLD:
                        correlation_flag = True
                        excluded_due_to = f"correlation ({correlations.idxmax()}, r={correlations.max():.2f})"
                elif not has_history:
                    job.note(f"{ticker}: insufficient price history for correlation check, sector cap still applied")

                sector = sectors.get(ticker)
                if excluded_due_to is None and sector:
                    current_count = sector_counts.get(sector, 0)
                    if current_count + 1 > MAX_NAMES_PER_SECTOR:
                        sector_cap_flag = True
                        excluded_due_to = f"sector_cap ({sector}, would be {current_count + 1} names > max {MAX_NAMES_PER_SECTOR})"

                if excluded_due_to is None:
                    accepted.append(ticker)
                    if sector:
                        sector_counts[sector] = sector_counts.get(sector, 0) + 1
                    final_rank = len(accepted)
                else:
                    final_rank = None

                results.append((ticker, correlation_flag, sector_cap_flag, excluded_due_to, final_rank))

            with conn:
                with conn.cursor() as cur:
                    # Delete existing rows for this score_date before
                    # inserting fresh ones, atomically — same fix as
                    # run_ml_ranking_cron.py's insert loop; see that
                    # script's comment for the full story.
                    cur.execute("DELETE FROM correlation_filtered_shortlist WHERE score_date = %s", (score_date,))
                    for ticker, correlation_flag, sector_cap_flag, excluded_due_to, final_rank in results:
                        cur.execute(
                            """
                            INSERT INTO correlation_filtered_shortlist
                                (ticker, score_date, correlation_flag, sector_cap_flag, excluded_due_to,
                                 final_rank, run_type, run_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (ticker, score_date, run_id) DO UPDATE
                                SET correlation_flag = EXCLUDED.correlation_flag,
                                    sector_cap_flag = EXCLUDED.sector_cap_flag,
                                    excluded_due_to = EXCLUDED.excluded_due_to,
                                    final_rank = EXCLUDED.final_rank
                            """,
                            (ticker, score_date, correlation_flag, sector_cap_flag, excluded_due_to,
                             final_rank, "cron", job.run_id),
                        )

            job.note(f"{len(accepted)}/{len(candidates)} candidates passed the filter")
            print(f"Filtered {len(candidates)} Stage 3 candidates -> {len(accepted)} passed")
            print("Accepted, in rank order:")
            for ticker, _, _, _, final_rank in sorted(
                (r for r in results if r[4] is not None), key=lambda r: r[4]
            ):
                print(f"  {final_rank:2d}. {ticker}")
            excluded = [r for r in results if r[4] is None]
            if excluded:
                print("Excluded:")
                for ticker, _, _, excluded_due_to, _ in excluded:
                    print(f"  {ticker:6s}  {excluded_due_to}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
