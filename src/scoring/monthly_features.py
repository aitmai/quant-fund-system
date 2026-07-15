"""
Shared monthly feature aggregation for Stage 3 (ML Ranking) — DESIGN.md
§3 Stage 3, §13 Phase 3.

Both the training script and the daily inference cron need to turn raw
weekly factor_scores rows into the SAME monthly-averaged feature
representation the model was actually fit on — this module is the one
place that logic lives, so training and serving can never quietly drift
apart (a classic source of silent model degradation: if inference fed
raw weekly z-scores while the model was trained on monthly averages,
predictions would be systematically wrong without any error being
raised anywhere).

FEATURE_COLUMNS defines both the feature set AND their order — the
trained model's coefficients are positional, so this list's order must
stay stable across training and inference. Change it only by retraining
immediately after.
"""

from datetime import date
from typing import Optional

import pandas as pd

FEATURE_COLUMNS = ["momentum_z", "quality_z", "value_z", "lowvol_z"]

# Matches src/scoring/compute_labels.py's TOP_DECILE_THRESHOLD default —
# kept as a separate constant (not imported) because this module governs
# the MONTHLY label recomputation, a deliberately different grain than
# compute_labels.py's weekly one; the two are conceptually related but
# not the same computation, so an independent constant avoids implying
# a tighter coupling than actually exists.
MONTHLY_TOP_DECILE_THRESHOLD = 0.90


def fetch_monthly_features(conn, as_of_date: date, months_back: Optional[int] = None) -> pd.DataFrame:
    """Aggregate factor_scores into one row per (ticker, month): the mean
    of each factor's weekly z-scores within that month, for months up to
    and including as_of_date's month.

    months_back, if given, limits to the trailing N months (a rolling
    window) — pass None for all available history (used by inference,
    which only needs the single most recent month; the training script
    applies its own 36-month trim on top of whatever this returns).

    Returns columns: ticker, month (first-of-month date), momentum_z,
    quality_z, value_z, lowvol_z — one row per (ticker, month).
    """
    query = """
        SELECT
            ticker,
            date_trunc('month', score_date)::date AS month,
            AVG(momentum_z) AS momentum_z,
            AVG(quality_z) AS quality_z,
            AVG(value_z) AS value_z,
            AVG(lowvol_z) AS lowvol_z
        FROM factor_scores
        WHERE score_date <= %s
    """
    params = [as_of_date]
    if months_back is not None:
        query += " AND score_date >= (date_trunc('month', %s::date) - make_interval(months => %s))"
        params.extend([as_of_date, months_back])
    query += " GROUP BY ticker, date_trunc('month', score_date) ORDER BY month, ticker"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        colnames = [desc[0] for desc in cur.description]
    return pd.DataFrame(rows, columns=colnames)


def fetch_monthly_labels(conn, as_of_date: date, months_back: Optional[int] = None) -> pd.DataFrame:
    """Aggregate training_labels into one row per (ticker, month): the
    mean forward_return_pct within that month, then re-derive a monthly
    top-decile label by ranking that mean CROSS-SECTIONALLY within each
    month — deliberately NOT an average of the already-computed weekly
    0/1 outperform_label flags (averaging binary flags would produce
    fractional labels like 0.4, which isn't a usable classification
    target and also isn't what "top decile" means at monthly grain).
    Recomputing the decile against monthly-averaged returns keeps the
    same "relative to peers that period" definition compute_labels.py
    uses, just applied at the coarser grain this model trains on.

    Returns columns: ticker, month, forward_return_pct (monthly mean),
    outperform_label (0/1, recomputed at monthly grain).
    """
    query = """
        SELECT ticker, date_trunc('month', score_date)::date AS month,
               AVG(forward_return_pct) AS forward_return_pct
        FROM training_labels
        WHERE score_date <= %s
    """
    params = [as_of_date]
    if months_back is not None:
        query += " AND score_date >= (date_trunc('month', %s::date) - make_interval(months => %s))"
        params.extend([as_of_date, months_back])
    query += " GROUP BY ticker, date_trunc('month', score_date)"

    with conn.cursor() as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
        colnames = [desc[0] for desc in cur.description]
    df = pd.DataFrame(rows, columns=colnames)
    if df.empty:
        df["outperform_label"] = pd.Series(dtype=int)
        return df

    # Cross-sectional top-decile rank WITHIN each month, across tickers.
    df["outperform_label"] = (
        df.groupby("month")["forward_return_pct"]
        .rank(pct=True, method="average")
        .ge(MONTHLY_TOP_DECILE_THRESHOLD)
        .astype(int)
    )
    return df


def build_monthly_dataset(conn, as_of_date: date, months_back: Optional[int] = None) -> pd.DataFrame:
    """Join monthly features + monthly labels on (ticker, month).
    Rows without a label (too recent — forward window hasn't concluded)
    are naturally dropped by the inner join; that's correct for
    TRAINING (can't train on an unknown outcome) but this function is
    also reused for the label-free case by callers who only need
    fetch_monthly_features() directly for inference, not this join.
    """
    features = fetch_monthly_features(conn, as_of_date, months_back)
    labels = fetch_monthly_labels(conn, as_of_date, months_back)
    merged = features.merge(labels[["ticker", "month", "outperform_label"]], on=["ticker", "month"], how="inner")
    return merged
