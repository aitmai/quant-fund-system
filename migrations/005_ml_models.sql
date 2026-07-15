-- Quant Fund System — ml_models table for Phase 3 (Stage 3 ML Ranking)
-- aitmai | 2026-07-15 — persists a trained model between GitHub Actions
-- runs. Runners are ephemeral (fresh VM every run, nothing survives on
-- disk between the monthly training job and the daily inference job),
-- so the trained model has to round-trip through somewhere durable.
--
-- Deliberately Postgres (BYTEA), not Supabase Storage — the model here
-- is small (a handful of logistic-regression coefficients, not a large
-- artifact), and src/db.py's own convention throughout this repo is one
-- connection, one style (plain psycopg2, no other services). Storage's
-- benefits (built for large binary objects, its own CDN/access-control
-- layer) don't apply at this size, and it would be the first thing in
-- this codebase that isn't reachable via DATABASE_URL alone. Revisit if
-- a future model type's serialized size becomes large enough that BYTEA
-- genuinely stops being the right fit.
--
-- Run against Supabase Postgres AFTER 001-004. Idempotent.

CREATE TABLE IF NOT EXISTS ml_models (
    model_version           TEXT PRIMARY KEY,
    trained_at               TIMESTAMPTZ DEFAULT NOW(),
    model_type                TEXT NOT NULL,           -- e.g. 'logistic_regression'
    retrain_mode              TEXT NOT NULL,           -- e.g. 'rolling_monthly_36mo'
    training_window_months    INT,
    feature_names              JSONB,                   -- ordered list, must match model_blob's expected input order
    coefficients                JSONB,                   -- {feature_name: weight, ...} — diagnostic/display only, NOT used for inference (model_blob is the source of truth)
    intercept                  NUMERIC,
    training_row_count         INT,
    training_month_range_start DATE,
    training_month_range_end   DATE,
    model_blob                  BYTEA NOT NULL,          -- pickled sklearn Pipeline (imputer + scaler + classifier)
    run_type                    TEXT CHECK (run_type IN ('cron', 'manual')),
    run_id                       TEXT,
    notes                        TEXT
);

CREATE INDEX IF NOT EXISTS idx_ml_models_trained_at ON ml_models (trained_at DESC);
