"""
Quality and value raw sub-metrics (DESIGN.md §3 Stage 2).

FLAGGED ASSUMPTION: DESIGN.md specifies Quality as "ROE, margin
stability, debt/equity" — but the `fundamentals` table (built in Phase 1,
before Phase 2 was scoped) has no margin-history column at all. The
closest available proxy is `earnings_variance` (trailing-4-quarter EPS
stdev, computed during fundamentals ingestion) — an earnings-stability
measure, not a margin-stability one. Substituted here rather than
silently building toward a metric that was never actually stored. If
margin stability specifically matters, that's a fundamentals-ingestion
schema change (new column, new XBRL concept), not a Stage 2 fix.

Sign convention: every raw sub-metric here is oriented so HIGHER = better,
matching momentum/low-vol's convention (see momentum_lowvol.py) — z-scoring
a mix of "higher is better" and "lower is better" columns without
normalizing first would silently invert half the factor's meaning.
  - ROE: already higher-is-better, used as-is.
  - debt_equity: lower is better (less leverage) -> negated.
  - earnings_variance: lower is better (more stable) -> negated.
  - fcf_yield: already higher-is-better, used as-is.
  - ev_ebitda: lower is better (cheaper valuation) -> negated.

Missing sub-metrics are tracked per ticker, per factor — a ticker missing
ONE of quality's three inputs still gets a quality score built from
whichever inputs it has (zscore.py averages only the available
sub-z-scores), but a ticker missing ALL of a factor's inputs gets NULL
for that factor entirely. Counting is the caller's job (run_factor_scoring.py)
since it needs the full active-universe denominator, not just what showed
up in the fundamentals table.
"""

from typing import Dict, Tuple

import pandas as pd

# Sub-metric raw-value dict names, kept consistent with zscore.py's
# expected input shape: {sub_metric_name: {ticker: raw_value}}
QUALITY_SUB_METRICS = ("roe", "debt_equity_inv", "earnings_stability")
VALUE_SUB_METRICS = ("fcf_yield", "ev_ebitda_inv")


def compute_quality_value_raw(fundamentals_panel: pd.DataFrame) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """Returns (quality_sub_raw, value_sub_raw), each shaped
    {sub_metric_name: {ticker: raw_value}} — NaN/None values are simply
    absent from the inner dict (not stored as NaN), so downstream code
    can tell "missing" from "zero" without a second check."""
    quality_sub_raw: Dict[str, Dict[str, float]] = {name: {} for name in QUALITY_SUB_METRICS}
    value_sub_raw: Dict[str, Dict[str, float]] = {name: {} for name in VALUE_SUB_METRICS}

    if fundamentals_panel.empty:
        return quality_sub_raw, value_sub_raw

    for _, row in fundamentals_panel.iterrows():
        ticker = row["ticker"]

        if pd.notna(row["roe"]):
            quality_sub_raw["roe"][ticker] = float(row["roe"])
        if pd.notna(row["debt_equity"]):
            quality_sub_raw["debt_equity_inv"][ticker] = -float(row["debt_equity"])
        if pd.notna(row["earnings_variance"]):
            quality_sub_raw["earnings_stability"][ticker] = -float(row["earnings_variance"])

        if pd.notna(row["fcf_yield"]):
            value_sub_raw["fcf_yield"][ticker] = float(row["fcf_yield"])
        if pd.notna(row["ev_ebitda"]):
            value_sub_raw["ev_ebitda_inv"][ticker] = -float(row["ev_ebitda"])

    return quality_sub_raw, value_sub_raw
