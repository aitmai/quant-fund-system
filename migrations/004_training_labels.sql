-- Quant Fund System — training_labels table for Phase 3 (Stage 3 ML Ranking)
-- aitmai | 2026-07-14 — Stage 3's model needs (features, label) pairs:
-- factor_scores already provides the features (point-in-time correct,
-- as_of-parameterized). This adds the label side: for each (ticker,
-- score_date), did that ticker's realized forward return land in the
-- TOP DECILE of that week's cross-sectional forward-return distribution?
--
-- Deliberately a SEPARATE table from factor_scores, not a reuse of its
-- already-NULL decile_rank column — factor_scores represents "what we
-- knew at the time" (point-in-time features), training_labels represents
-- "what actually happened" (only knowable in hindsight, once the forward
-- window has concluded). Keeping them separate is the standard ML
-- train/label split and avoids overloading decile_rank's already-
-- documented open question (see src/scoring/run_factor_scoring.py) with
-- a second, different meaning.
--
-- KNOWN LIMITATION (accepted for v1, see conversation 2026-07-14):
-- backfilled factor_scores (and therefore these labels) score against
-- TODAY's active universe applied retroactively, not actual historical
-- S&P 500 membership at each past date — universe_changes only tracks
-- membership from whenever this project started syncing, not 3 years
-- back. This is real survivorship bias in the v1 training set. Revisit
-- if/when a historical constituents data source is added.
--
-- Run against Supabase Postgres AFTER 001, 002, and 003. Idempotent.

CREATE TABLE IF NOT EXISTS training_labels (
    ticker                  TEXT NOT NULL,
    score_date              DATE NOT NULL,
    horizon_trading_days    INT NOT NULL,
    forward_return_pct      NUMERIC,
    outperform_label        INT CHECK (outperform_label IN (0, 1)),
    computed_at             TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (ticker, score_date, horizon_trading_days)
);

CREATE INDEX IF NOT EXISTS idx_training_labels_score_date ON training_labels (score_date);

-- Backfill runs are tagged distinctly in job_runs (triggered_by='backfill')
-- so they're auditable and separable from real weekly Stage 2 cron
-- history — no schema change needed for that, job_runs.triggered_by is
-- already a free-text column.
