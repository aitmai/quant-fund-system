"""
Daily Stage 3 inference — DESIGN.md §3 Stage 3, §13 Phase 3.

Runs every weekday evening as part of the daily-picks pipeline (Stage
2 factor scoring -> THIS -> Stage 4 correlation filter -> Stage 5 risk
sizing -> top 10 in daily_picks). Does NOT train anything — loads
whatever model scripts/train_ml_ranking_model.py most recently
persisted to ml_models, and scores today's active universe against it.

Feature computation reuses src/scoring/monthly_features.py's
fetch_monthly_features() with months_back=1 — the SAME trailing-month
averaging the model was trained on, not that day's raw weekly z-score.
This is deliberate and important: feeding the model something other
than what it was trained on would be train/serve skew, and would
silently produce meaningless probabilities with no error anywhere to
catch it.

Usage:
    python scripts/run_ml_ranking_cron.py
    python scripts/run_ml_ranking_cron.py --score-date 2026-07-15
"""

import argparse
import json
import pickle
import sys
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.job_run import JobRun
from src.scoring.monthly_features import FEATURE_COLUMNS, fetch_monthly_features


def _load_latest_model(cur):
    cur.execute(
        """
        SELECT model_version, model_blob, feature_names
        FROM ml_models
        ORDER BY trained_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    if not row:
        print(
            "ERROR: no model found in ml_models. Run scripts/train_ml_ranking_model.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    model_version, model_blob, feature_names_json = row
    feature_names = feature_names_json if isinstance(feature_names_json, list) else json.loads(feature_names_json)
    if feature_names != FEATURE_COLUMNS:
        print(
            f"ERROR: model {model_version}'s feature_names {feature_names} don't match "
            f"the current FEATURE_COLUMNS {FEATURE_COLUMNS} — src/scoring/monthly_features.py "
            f"changed since this model was trained. Retrain before running inference.",
            file=sys.stderr,
        )
        sys.exit(1)
    pipeline = pickle.loads(bytes(model_blob))
    return model_version, pipeline


def main():
    parser = argparse.ArgumentParser(description="Score today's candidates against the latest Stage 3 model.")
    parser.add_argument("--score-date", help="Score as of this date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()
    score_date = date.fromisoformat(args.score_date) if args.score_date else date.today()

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            model_version, pipeline = _load_latest_model(cur)

        with JobRun(conn, stage="ml_ranking", run_type="cron", triggered_by=args.triggered_by) as job:
            features = fetch_monthly_features(conn, score_date, months_back=1)
            # months_back=1 can return the trailing month AND the current
            # partial month depending on where score_date falls — keep
            # only the most recent month per ticker, so each ticker gets
            # exactly one row scored (matches training grain: one row
            # per ticker-month, not one row per ticker across two months).
            if features.empty:
                print(f"ERROR: no factor_scores found on/before {score_date} to build features from.", file=sys.stderr)
                sys.exit(1)
            latest_month = features["month"].max()
            features = features[features["month"] == latest_month].copy()

            X = features[FEATURE_COLUMNS]
            p_outperform = pipeline.predict_proba(X)[:, 1]
            features["p_outperform"] = p_outperform

            classifier = pipeline.named_steps["classifier"]
            coefficients = dict(zip(FEATURE_COLUMNS, classifier.coef_[0].tolist()))

            with conn:
                with conn.cursor() as cur:
                    # Delete any existing rows for this score_date BEFORE
                    # inserting fresh ones — within the same transaction,
                    # so a rerun atomically REPLACES the day's scores
                    # instead of accumulating a second copy alongside the
                    # first. Read-side queries already defend against
                    # this (scoped to "most recent run"), but that only
                    # papers over duplicates after the fact; this stops
                    # them from being written at all, which is the real
                    # fix (found live 2026-07-15 — see run_risk_sizing_cron.py's
                    # and get_signal_vs_execution's fix notes for the
                    # full multi-stage story this bug caused).
                    cur.execute("DELETE FROM ml_rankings WHERE score_date = %s", (score_date,))
                    for row in features.itertuples(index=False):
                        cur.execute(
                            """
                            INSERT INTO ml_rankings (ticker, score_date, p_outperform, model_version,
                                                      feature_importance, run_type, run_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (ticker, score_date, run_id) DO UPDATE
                                SET p_outperform = EXCLUDED.p_outperform
                            """,
                            (row.ticker, score_date, float(row.p_outperform), model_version,
                             json.dumps(coefficients), "cron", job.run_id),
                        )

            job.note(f"scored {len(features)} tickers using model {model_version} (features from month {latest_month})")
            top_10 = features.sort_values("p_outperform", ascending=False).head(10)
            print(f"Scored {len(features)} tickers using model {model_version} (feature month: {latest_month})")
            print("Top 10 by P(outperform):")
            for row in top_10.itertuples(index=False):
                print(f"  {row.ticker:6s}  {row.p_outperform:.4f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
