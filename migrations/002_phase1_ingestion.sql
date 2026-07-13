-- Quant Fund System — Phase 1 Schema Additions
-- aitmai | Data Ingestion (Stage 1 + backfill/maintenance cron)
-- Run against Supabase Postgres AFTER 001_initial_schema.sql. Idempotent: safe to re-run.

-- ============================================================
-- Price provider selection (backend supports both Tiingo and
-- yfinance; this column is what a future GUI toggle — Phase 8 —
-- will write to. For now it defaults to yfinance since it needs
-- no API key to get ingestion running immediately.)
-- ============================================================

ALTER TABLE ingestion_config
    ADD COLUMN IF NOT EXISTS active_provider TEXT
        CHECK (active_provider IN ('tiingo', 'yfinance'))
        DEFAULT 'yfinance';

-- Only meaningful for data_type='price' (fundamentals has one provider: FMP).
-- Set explicitly so the row's semantics aren't left implicit.
UPDATE ingestion_config SET active_provider = 'yfinance' WHERE data_type = 'price';
UPDATE ingestion_config SET active_provider = NULL WHERE data_type = 'fundamentals';

-- ============================================================
-- Per-ticker audit: which provider actually served the last
-- successful fetch. Populated by the fallback logic in
-- src/providers/provider_factory.py — if the active provider
-- fails, the ingestion module automatically tries the other one
-- before giving up, and records which one worked.
-- ============================================================

ALTER TABLE ingestion_state
    ADD COLUMN IF NOT EXISTS last_provider_used TEXT;

-- ============================================================
-- Daily + rolling-30-day usage ledger, tracked independently per
-- data_type (DESIGN.md §5 — price and fundamentals have separate
-- budgets so one can't starve the other). Replaces relying on
-- ingestion_config.running_bandwidth_used_mb as a single running
-- total — a per-day row lets us both enforce "budget per day" and
-- sum a trailing 30-day window for FMP's bandwidth cap.
-- ============================================================

CREATE TABLE IF NOT EXISTS ingestion_daily_usage (
    data_type          TEXT NOT NULL CHECK (data_type IN ('price', 'fundamentals')),
    usage_date         DATE NOT NULL,
    calls_used         INT NOT NULL DEFAULT 0,
    bandwidth_used_mb  NUMERIC NOT NULL DEFAULT 0,
    PRIMARY KEY (data_type, usage_date)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_daily_usage_date ON ingestion_daily_usage (usage_date);

-- ============================================================
-- Fundamentals need more than one FMP endpoint per ticker
-- (ratios, key-metrics, income-statement) to populate every
-- column in the `fundamentals` table. Reflect that in
-- calls_per_ticker so budget math (daily_budget / calls_per_ticker
-- = tickers/day) stays accurate instead of silently overspending.
-- ============================================================

UPDATE ingestion_config SET calls_per_ticker = 3 WHERE data_type = 'fundamentals';
