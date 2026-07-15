"""
Train the Stage 3 ranking model — DESIGN.md §3 Stage 3, §13 Phase 3.

Rolling-window logistic regression, retrained monthly (a deliberate
departure from DESIGN.md's 6-month default — see conversation 2026-07-15:
with monthly-averaged features the training set is small, a logistic fit
takes well under a second, and monthly retraining lets the model adapt
faster than the original 6-month cadence, which was sized for a much
larger weekly-row training set):

  1. Pull the trailing 36 months of (ticker, month) feature/label pairs
     via src/scoring/monthly_features.py — shared with the daily
     inference script so features can never drift between train and
     serve.
  2. Fit sklearn Pipeline(SimpleImputer(median) -> StandardScaler ->
     LogisticRegression) on it.
  3. Serialize the fitted pipeline and persist it to ml_models (BYTEA),
     versioned so every ml_rankings row traces back to exactly which
     model produced it.

KNOWN v1 LIMITATIONS (explicit open items, same pattern as this repo's
other phase addenda):
  - Missing factor coverage (per the 2026-07-15 backfill run: momentum
    376/501 tickers, lowvol 377/501, value 344/501, quality 501/501) is
    handled by median imputation inside the pipeline, not by dropping
    rows. DESIGN.md doesn't specify a missing-data policy for Stage 3;
    median imputation was chosen as a defensible default that avoids
    throwing away most of the training set (momentum/value coverage is
    under 75%), but it does mean a ticker with genuinely no momentum
    history gets treated as "average momentum" rather than "unknown" —
    revisit if a specific policy becomes a stated requirement.
  - No train/test split or holdout evaluation yet — this prints basic
    in-sample sanity metrics (accuracy, positive rate) only, matching
    Phase 3's own milestone bar ("model trains without error and
    produces a ranked shortlist that passes a basic sanity check").
    Real backtested evaluation belongs to Phase 10 (Backtest Engine),
    which DESIGN.md deliberately places last.
  - No multi-year survivorship-bias correction — same known limitation
    already documented in migrations/004_training_labels.sql, inherited
    here since this trains on that same labeled data.

Usage:
    python scripts/train_ml_ranking_model.py
    python scripts/train_ml_ranking_model.py --as-of-date 2026-07-15
"""

import argparse
import json
import pickle
import sys
import uuid
from datetime import date

sys.path.insert(0, ".")

from dotenv import load_dotenv
load_dotenv()

import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from src.db import get_connection
from src.job_run import JobRun
from src.scoring.monthly_features import FEATURE_COLUMNS, build_monthly_dataset

TRAINING_WINDOW_MONTHS = 36
MODEL_TYPE = "logistic_regression"
RETRAIN_MODE = f"rolling_monthly_{TRAINING_WINDOW_MONTHS}mo"


def main():
    parser = argparse.ArgumentParser(description="Train the Stage 3 ranking model (Phase 3).")
    parser.add_argument("--as-of-date", help="Train using data through this date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--triggered-by", default=None)
    args = parser.parse_args()
    as_of_date = date.fromisoformat(args.as_of_date) if args.as_of_date else date.today()

    conn = get_connection()
    try:
        with JobRun(conn, stage="train_ml_ranking_model", run_type="manual", triggered_by=args.triggered_by) as job:
            dataset = build_monthly_dataset(conn, as_of_date, months_back=TRAINING_WINDOW_MONTHS)

            if dataset.empty:
                print("ERROR: no (ticker, month) rows with both features and a label were found.", file=sys.stderr)
                sys.exit(1)

            if dataset["outperform_label"].nunique() < 2:
                print(
                    f"ERROR: training data has only one label class present "
                    f"({dataset['outperform_label'].unique().tolist()}) — cannot fit a classifier on it. "
                    f"This can happen with too little history; try a later --as-of-date once more months exist.",
                    file=sys.stderr,
                )
                sys.exit(1)

            X = dataset[FEATURE_COLUMNS]
            y = dataset["outperform_label"]

            pipeline = Pipeline([
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("classifier", LogisticRegression(max_iter=1000)),
            ])
            pipeline.fit(X, y)

            train_accuracy = pipeline.score(X, y)
            positive_rate = float(y.mean())
            predicted_positive_rate = float(pipeline.predict(X).mean())

            classifier = pipeline.named_steps["classifier"]
            coefficients = dict(zip(FEATURE_COLUMNS, classifier.coef_[0].tolist()))
            intercept = float(classifier.intercept_[0])

            model_version = f"logreg-{as_of_date.isoformat()}-{uuid.uuid4().hex[:8]}"
            model_blob = pickle.dumps(pipeline)

            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ml_models
                            (model_version, trained_at, model_type, retrain_mode, training_window_months,
                             feature_names, coefficients, intercept, training_row_count,
                             training_month_range_start, training_month_range_end, model_blob,
                             run_type, run_id, notes)
                        VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            model_version, MODEL_TYPE, RETRAIN_MODE, TRAINING_WINDOW_MONTHS,
                            json.dumps(FEATURE_COLUMNS), json.dumps(coefficients), intercept, len(dataset),
                            dataset["month"].min(), dataset["month"].max(), model_blob,
                            "manual", job.run_id,
                            f"train_accuracy={train_accuracy:.4f} positive_rate={positive_rate:.4f}",
                        ),
                    )

            job.note(
                f"trained on {len(dataset)} ticker-months "
                f"({dataset['month'].min()} to {dataset['month'].max()}), "
                f"train_accuracy={train_accuracy:.4f}"
            )

            print(f"Trained model {model_version}")
            print(f"  training rows (ticker-months): {len(dataset)}")
            print(f"  month range: {dataset['month'].min()} to {dataset['month'].max()}")
            print(f"  train accuracy: {train_accuracy:.4f}")
            print(f"  actual positive rate: {positive_rate:.4f}  (expect ~0.10, top-decile by construction)")
            print(f"  predicted positive rate: {predicted_positive_rate:.4f}")
            print(f"  coefficients: {coefficients}")
            print(f"  intercept: {intercept:.4f}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
