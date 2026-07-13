-- Quant Fund System — Initial Schema
-- aitmai | Phase 0/1 scaffold, matches DESIGN.md §6
-- Run against Supabase Postgres. Idempotent: safe to re-run.

-- ============================================================
-- 6.1 Reference / Universe
-- ============================================================

CREATE TABLE IF NOT EXISTS universe (
    ticker              TEXT PRIMARY KEY,
    company_name        TEXT,
    market_cap          NUMERIC,
    avg_dollar_volume    NUMERIC,
    exchange            TEXT,
    sector              TEXT,
    is_active           BOOLEAN DEFAULT TRUE,
    source              TEXT CHECK (source IN ('auto', 'manual')) DEFAULT 'auto',
    added_date          DATE DEFAULT CURRENT_DATE
);

CREATE TABLE IF NOT EXISTS universe_changes (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    change_type         TEXT CHECK (change_type IN ('added', 'removed')),
    change_date         DATE NOT NULL,
    source_index        TEXT,
    detected_by         TEXT CHECK (detected_by IN ('cron', 'manual')),
    reason              TEXT
);

CREATE TABLE IF NOT EXISTS price_history (
    ticker              TEXT NOT NULL REFERENCES universe(ticker),
    date                DATE NOT NULL,
    open                NUMERIC,
    high                NUMERIC,
    low                 NUMERIC,
    close               NUMERIC,
    volume              BIGINT,
    adj_close           NUMERIC,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker              TEXT NOT NULL REFERENCES universe(ticker),
    report_date         DATE NOT NULL,
    filed_date          DATE NOT NULL,  -- critical for point-in-time backtest correctness, see DESIGN.md §8.2
    roe                 NUMERIC,
    ev_ebitda           NUMERIC,
    fcf_yield           NUMERIC,
    debt_equity         NUMERIC,
    earnings_variance   NUMERIC,
    PRIMARY KEY (ticker, report_date)
);

-- ============================================================
-- 6.2 Ingestion Control
-- ============================================================

CREATE TABLE IF NOT EXISTS ingestion_state (
    ticker              TEXT NOT NULL,
    data_type           TEXT CHECK (data_type IN ('price', 'fundamentals')),
    last_fetched_date   DATE,
    fetch_status        TEXT CHECK (fetch_status IN ('pending', 'complete', 'failed')) DEFAULT 'pending',
    last_attempt_at     TIMESTAMPTZ,
    retry_count         INT DEFAULT 0,
    priority_rank       INT DEFAULT 100,
    PRIMARY KEY (ticker, data_type)
);

CREATE TABLE IF NOT EXISTS ingestion_config (
    data_type                TEXT PRIMARY KEY CHECK (data_type IN ('price', 'fundamentals')),
    daily_budget             INT NOT NULL,
    calls_per_ticker         INT DEFAULT 1,
    monthly_bandwidth_cap_mb NUMERIC,       -- FMP free tier: 500 MB / trailing 30 days, see DESIGN.md §5
    running_bandwidth_used_mb NUMERIC DEFAULT 0
);

-- Seed sane defaults — adjust to your actual provider limits before running ingestion.
INSERT INTO ingestion_config (data_type, daily_budget, calls_per_ticker, monthly_bandwidth_cap_mb)
VALUES
    ('price', 400, 1, NULL),
    ('fundamentals', 225, 1, 500)  -- 225, not 250: leaves a safety margin per DESIGN.md §5 backfill design
ON CONFLICT (data_type) DO NOTHING;

-- ============================================================
-- 6.3 Pipeline Output (Stages 2–6)
-- ============================================================

CREATE TABLE IF NOT EXISTS factor_scores (
    ticker              TEXT NOT NULL,
    score_date          DATE NOT NULL,
    momentum_z          NUMERIC,
    quality_z           NUMERIC,
    value_z             NUMERIC,
    lowvol_z            NUMERIC,
    decile_rank         INT,
    run_type            TEXT CHECK (run_type IN ('cron', 'manual')),
    run_id              TEXT,
    PRIMARY KEY (ticker, score_date, run_id)
);

CREATE TABLE IF NOT EXISTS ml_rankings (
    ticker              TEXT NOT NULL,
    score_date          DATE NOT NULL,
    p_outperform        NUMERIC,
    model_version       TEXT,
    feature_importance  JSONB,
    run_type            TEXT CHECK (run_type IN ('cron', 'manual')),
    run_id              TEXT,
    PRIMARY KEY (ticker, score_date, run_id)
);

CREATE TABLE IF NOT EXISTS correlation_filtered_shortlist (
    ticker              TEXT NOT NULL,
    score_date          DATE NOT NULL,
    correlation_flag    BOOLEAN DEFAULT FALSE,
    sector_cap_flag     BOOLEAN DEFAULT FALSE,
    excluded_due_to     TEXT,
    final_rank          INT,
    run_type            TEXT CHECK (run_type IN ('cron', 'manual')),
    run_id              TEXT,
    PRIMARY KEY (ticker, score_date, run_id)
);

CREATE TABLE IF NOT EXISTS daily_picks (
    ticker              TEXT NOT NULL,
    trade_date          DATE NOT NULL,
    position_size       NUMERIC,
    target_vol          NUMERIC,
    realized_vol        NUMERIC,
    dollar_allocated    NUMERIC,
    run_type            TEXT CHECK (run_type IN ('cron', 'manual')),
    run_id              TEXT,
    executed            BOOLEAN DEFAULT FALSE,
    PRIMARY KEY (ticker, trade_date, run_id)
);

CREATE TABLE IF NOT EXISTS portfolio_risk_snapshots (
    snapshot_date            DATE NOT NULL,
    portfolio_vol            NUMERIC,
    portfolio_beta           NUMERIC,
    sector_concentration     JSONB,
    factor_exposure          JSONB,
    top_correlated_clusters  JSONB,
    run_type                 TEXT CHECK (run_type IN ('cron', 'manual')),
    run_id                   TEXT,
    PRIMARY KEY (snapshot_date, run_id)
);

CREATE TABLE IF NOT EXISTS vol_hedge_state (
    date                     DATE PRIMARY KEY,
    vix_futures_contract_month TEXT,
    vix_futures_price        NUMERIC,
    roll_cost_running        NUMERIC DEFAULT 0,
    implied_vol              NUMERIC,
    realized_vol             NUMERIC,
    garch_forecast           NUMERIC,
    term_structure_signal    TEXT,
    spy_put_strike           NUMERIC,
    spy_put_dte              INT,
    spy_put_delta            NUMERIC,
    spy_put_premium          NUMERIC,
    theta_decay_running      NUMERIC DEFAULT 0,
    hedge_action             TEXT,
    hedge_dollar_amount      NUMERIC,
    contracts_held           INT
);

-- ============================================================
-- 6.4 Exit Rules
-- ============================================================

CREATE TABLE IF NOT EXISTS exit_rules_config (
    rule_type           TEXT PRIMARY KEY CHECK (rule_type IN ('min_hold', 'requalification', 'stop_loss')),
    threshold_value     NUMERIC,
    lookback_period     TEXT
);

INSERT INTO exit_rules_config (rule_type, threshold_value, lookback_period)
VALUES
    ('min_hold', 4, 'quarters'),
    ('requalification', NULL, 'quarterly'),
    ('stop_loss', -0.25, NULL)
ON CONFLICT (rule_type) DO NOTHING;

CREATE TABLE IF NOT EXISTS exit_signals (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    signal_date         DATE NOT NULL,
    rule_triggered      TEXT,
    current_score       NUMERIC,
    current_rank        INT,
    unrealized_pnl_pct  NUMERIC,
    run_id              TEXT
);

-- ============================================================
-- 6.5 Execution & Performance
-- ============================================================

CREATE TABLE IF NOT EXISTS fund_metadata (
    id                      INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- single-row table
    fund_start_date         DATE,
    initial_capital         NUMERIC,
    total_cash_invested     NUMERIC,
    last_updated            TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trades (
    trade_id            TEXT PRIMARY KEY,
    trade_date          DATE NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT CHECK (side IN ('buy', 'sell')),
    sleeve              TEXT CHECK (sleeve IN ('long', 'hedge')),
    instrument_type     TEXT CHECK (instrument_type IN ('equity', 'vix_future', 'spy_put')),
    quantity            NUMERIC,
    price               NUMERIC,
    dollar_amount       NUMERIC,
    commission_fees     NUMERIC DEFAULT 0,
    run_id              TEXT,
    notes               TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    ticker              TEXT NOT NULL,
    sleeve              TEXT CHECK (sleeve IN ('long', 'hedge')),
    quantity            NUMERIC,
    avg_cost_basis      NUMERIC,
    current_price       NUMERIC,
    market_value        NUMERIC,
    unrealized_pnl      NUMERIC,
    purchase_date       DATE,
    last_updated        TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (ticker, sleeve)
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    snapshot_date           DATE PRIMARY KEY,
    total_nav               NUMERIC,
    long_sleeve_value       NUMERIC,
    hedge_sleeve_value      NUMERIC,
    cash_balance            NUMERIC,
    daily_pnl               NUMERIC,
    daily_return_pct        NUMERIC,
    cumulative_return_pct   NUMERIC
);

CREATE TABLE IF NOT EXISTS realized_pnl (
    id                      BIGSERIAL PRIMARY KEY,
    ticker                  TEXT NOT NULL,
    sleeve                  TEXT CHECK (sleeve IN ('long', 'hedge')),
    open_date               DATE,
    close_date              DATE,
    holding_period_days     INT,
    cost_basis              NUMERIC,
    proceeds                NUMERIC,
    realized_gain_loss      NUMERIC,
    return_pct              NUMERIC,
    exit_reason             TEXT
);

-- ============================================================
-- 6.6 Corporate Actions
-- ============================================================

CREATE TABLE IF NOT EXISTS corporate_actions (
    id                  BIGSERIAL PRIMARY KEY,
    ticker              TEXT NOT NULL,
    action_date         DATE NOT NULL,
    action_type         TEXT CHECK (action_type IN ('merger', 'spinoff', 'delisting', 'bankruptcy')),
    resulting_ticker    TEXT,
    cash_or_stock_terms JSONB,
    detected_by         TEXT CHECK (detected_by IN ('cron', 'manual'))
);

-- ============================================================
-- 6.7 Operational / Audit
-- ============================================================

CREATE TABLE IF NOT EXISTS job_runs (
    run_id              TEXT PRIMARY KEY,
    stage               TEXT,
    run_type            TEXT CHECK (run_type IN ('cron', 'manual')),
    triggered_by         TEXT,
    start_time           TIMESTAMPTZ,
    end_time             TIMESTAMPTZ,
    status               TEXT,
    error_message        TEXT
);

-- ============================================================
-- 6.8 Backtesting (fully isolated from production tables)
-- ============================================================

CREATE TABLE IF NOT EXISTS backtest_runs (
    backtest_id         TEXT PRIMARY KEY,
    run_name            TEXT,
    start_date          DATE,
    end_date            DATE,
    model_version       TEXT,
    config_snapshot     JSONB,
    is_holdout_run      BOOLEAN DEFAULT FALSE,
    status              TEXT DEFAULT 'queued',
    progress_pct        NUMERIC DEFAULT 0,
    current_sim_date    DATE,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    error_message       TEXT
);

CREATE TABLE IF NOT EXISTS backtest_segments (
    segment_id                  TEXT PRIMARY KEY,
    backtest_id                 TEXT NOT NULL REFERENCES backtest_runs(backtest_id),
    segment_index                INT NOT NULL,
    segment_start_date           DATE NOT NULL,
    segment_end_date             DATE NOT NULL,
    status                      TEXT CHECK (status IN ('queued', 'running', 'complete', 'failed')) DEFAULT 'queued',
    locked_by                   TEXT,
    started_at                  TIMESTAMPTZ,
    completed_at                 TIMESTAMPTZ,
    ending_nav                  NUMERIC,
    ending_positions_snapshot_ref TEXT
);

CREATE TABLE IF NOT EXISTS backtest_daily_picks (
    backtest_id         TEXT NOT NULL REFERENCES backtest_runs(backtest_id),
    ticker              TEXT NOT NULL,
    sim_date            DATE NOT NULL,
    position_size       NUMERIC,
    target_vol          NUMERIC,
    realized_vol        NUMERIC,
    dollar_allocated    NUMERIC,
    PRIMARY KEY (backtest_id, ticker, sim_date)
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    backtest_id         TEXT NOT NULL REFERENCES backtest_runs(backtest_id),
    trade_id            TEXT NOT NULL,
    sim_date            DATE NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT,
    sleeve              TEXT,
    quantity            NUMERIC,
    price               NUMERIC,
    dollar_amount       NUMERIC,
    transaction_cost    NUMERIC DEFAULT 0,
    PRIMARY KEY (backtest_id, trade_id)
);

CREATE TABLE IF NOT EXISTS backtest_portfolio_snapshots (
    backtest_id             TEXT NOT NULL REFERENCES backtest_runs(backtest_id),
    sim_date                DATE NOT NULL,
    total_nav               NUMERIC,
    long_sleeve_value       NUMERIC,
    hedge_sleeve_value      NUMERIC,
    daily_return_pct        NUMERIC,
    cumulative_return_pct   NUMERIC,
    PRIMARY KEY (backtest_id, sim_date)
);

CREATE TABLE IF NOT EXISTS backtest_model_versions (
    model_version_id            TEXT PRIMARY KEY,
    backtest_id                 TEXT NOT NULL REFERENCES backtest_runs(backtest_id),
    trained_on_data_through     DATE,
    active_from_date            DATE,
    active_to_date              DATE,
    model_artifact_path         TEXT  -- Supabase Storage path, never local disk — see DESIGN.md §8.4
);

CREATE TABLE IF NOT EXISTS backtest_metrics (
    backtest_id         TEXT PRIMARY KEY REFERENCES backtest_runs(backtest_id),
    cagr                NUMERIC,
    sharpe_ratio        NUMERIC,
    max_drawdown        NUMERIC,
    win_rate            NUMERIC,
    benchmark_return    NUMERIC,
    alpha               NUMERIC,
    beta                NUMERIC
);

CREATE TABLE IF NOT EXISTS backtest_experiment_log (
    experiment_id       TEXT PRIMARY KEY,
    backtest_id         TEXT REFERENCES backtest_runs(backtest_id),
    config_snapshot     JSONB,
    in_sample_metrics   JSONB,
    is_holdout_run      BOOLEAN DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Indexes for the query patterns the design actually uses
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_price_history_date ON price_history (date);
CREATE INDEX IF NOT EXISTS idx_fundamentals_filed_date ON fundamentals (filed_date);  -- point-in-time filter, DESIGN.md §8.2
CREATE INDEX IF NOT EXISTS idx_factor_scores_date ON factor_scores (score_date);
CREATE INDEX IF NOT EXISTS idx_daily_picks_date ON daily_picks (trade_date);
CREATE INDEX IF NOT EXISTS idx_ingestion_state_status ON ingestion_state (fetch_status, priority_rank);
CREATE INDEX IF NOT EXISTS idx_backtest_segments_status ON backtest_segments (backtest_id, status, segment_index);
