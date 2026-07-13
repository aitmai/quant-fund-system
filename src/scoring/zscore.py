"""
Sector-relative z-scoring (per your 2026-07-13 design call — DESIGN.md
itself doesn't specify universe-wide vs. sector-relative; sector-relative
was chosen since comparing Quality within Tech to Tech is more meaningful
than comparing it to Utilities).

Composite factors (quality_z, value_z) are built by z-scoring each raw
sub-metric WITHIN SECTOR independently, then averaging a ticker's
available sub-z-scores — "available" meaning whichever sub-metrics that
ticker actually has data for, not requiring all of them. A ticker with
2 of 3 quality inputs still gets a quality_z built from those 2, not a
NULL.
"""

from typing import Dict, List, Optional

import pandas as pd


def sector_zscore(raw: Dict[str, float], sector_map: Dict[str, str]) -> Dict[str, float]:
    """z-score of `raw` values, computed WITHIN each sector group
    (sector_map: {ticker: sector}) rather than across the whole universe.

    A sector group with fewer than 2 tickers having a raw value can't be
    meaningfully z-scored (no dispersion to measure against) — those
    tickers are simply absent from the returned dict, same "absent means
    missing, not zero" convention as quality_value.py.
    """
    if not raw:
        return {}

    df = pd.DataFrame(
        [{"ticker": t, "raw": v, "sector": sector_map.get(t, "Unknown")} for t, v in raw.items()]
    )

    def _zscore_group(group: pd.Series) -> pd.Series:
        std = group.std(ddof=1)
        if pd.isna(std) or len(group) < 2:
            return pd.Series([None] * len(group), index=group.index)
        if std == 0:
            # No dispersion within the sector — every ticker is
            # identical to its peers, which fairly means z=0, not "can't
            # compute" (unlike the n<2 case above, this IS a real result).
            return pd.Series([0.0] * len(group), index=group.index)
        return (group - group.mean()) / std

    df["z"] = df.groupby("sector")["raw"].transform(_zscore_group)
    return {row["ticker"]: row["z"] for _, row in df.iterrows() if pd.notna(row["z"])}


def average_available_zscores(sub_zscore_dicts: List[Dict[str, float]]) -> Dict[str, float]:
    """Averages a ticker's available sub-metric z-scores — only over the
    ones that exist for that ticker, not requiring all of them. A ticker
    absent from every sub-dict is simply absent from the result (that
    factor is NULL for them), not defaulted to 0 — an imputed neutral
    score would tell Stage 3's model "this ticker is average" when the
    truth is "we don't know," which is a materially different signal."""
    totals: Dict[str, float] = {}
    counts: Dict[str, int] = {}

    for sub_dict in sub_zscore_dicts:
        for ticker, z in sub_dict.items():
            totals[ticker] = totals.get(ticker, 0.0) + z
            counts[ticker] = counts.get(ticker, 0) + 1

    return {ticker: totals[ticker] / counts[ticker] for ticker in totals}
