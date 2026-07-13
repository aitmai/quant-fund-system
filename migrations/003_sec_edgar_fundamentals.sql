-- Quant Fund System — Swap fundamentals sourcing from FMP to SEC EDGAR
-- aitmai | 2026-07-13 — FMP's free tier turned out to plan-gate ratios,
-- key-metrics, AND income-statement (confirmed via live 402s on every
-- S&P 500 ticker tested). SEC EDGAR's XBRL API is completely free, no
-- key, no plan tiers — the tradeoff is raw filed numbers instead of
-- pre-computed ratios, computed in src/providers/sec_edgar_provider.py.
-- Run against Supabase Postgres AFTER 001 and 002. Idempotent.

-- ============================================================
-- CIK (SEC's Central Index Key) — required to look up a company's
-- filings on EDGAR. Captured directly from Wikipedia's S&P 500
-- constituents table during universe sync (it's already a column
-- there) — see src/universe/index_sources.py.
-- ============================================================

ALTER TABLE universe ADD COLUMN IF NOT EXISTS cik TEXT;
CREATE INDEX IF NOT EXISTS idx_universe_cik ON universe (cik);

-- ============================================================
-- Fundamentals budget: EDGAR has no daily request cap, only a
-- 10-requests-per-second rate limit (paced in code, not budgeted per
-- day) — and one API call per ticker returns a company's ENTIRE XBRL
-- history, not one endpoint's worth of one metric. calls_per_ticker
-- drops from 3 (FMP: ratios + key-metrics + income-statement) to 1.
-- daily_budget is set generously high since it's no longer the real
-- constraint; monthly_bandwidth_cap_mb is cleared since EDGAR publishes
-- no such limit (that 500MB figure was FMP-specific).
-- ============================================================

UPDATE ingestion_config
SET daily_budget = 5000, calls_per_ticker = 1, monthly_bandwidth_cap_mb = NULL
WHERE data_type = 'fundamentals';
