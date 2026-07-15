"""
All GUI database queries, organized by screen (matches DESIGN.md §11's
11.1-11.9 numbering). Kept separate from routes.py so a route handler
never contains raw SQL — makes it obvious at a glance which queries exist
and which tables each screen actually touches.

Every function takes an open `conn` and returns plain dicts/lists —
psycopg2's RealDictCursor (set up in db.py) does the dict conversion, so
nothing here needs to know about cursor mechanics.

Tables that no phase has populated yet (positions, trades, exit_signals,
vol_hedge_state, backtest_runs, ...) are queried for real, the same as
everything else — they just currently return empty results. That's the
honest state of the system today, not a placeholder.
"""

from datetime import date, timedelta

# Pipeline stages, in Stage order, mapped to the exact job_runs.stage
# value each cron script writes (confirmed against scripts/run_*.py —
# see JobRun(conn, stage="...") calls). Stages with no script yet
# (ml_ranking onward) use the key their eventual Phase 3-6 script is
# expected to use; they'll show as 'idle' until that phase is built,
# which is the honest current state, not a placeholder guess.
PIPELINE_STAGES = [
    ("universe_sync", "Stage 1 · Universe Sync"),
    ("ingestion_price", "Stage 1 · Price Ingestion"),
    ("ingestion_fundamentals", "Stage 1 · Fundamentals Ingestion"),
    ("factor_scoring", "Stage 2 · Factor Scoring"),
    ("ml_ranking", "Stage 3 · ML Ranking"),
    ("correlation_filter", "Stage 4 · Correlation Filter"),
    ("risk_sizing", "Stage 5 · Risk Sizing"),
    ("portfolio_risk", "Stage 6 · Portfolio Risk"),
]

TRAILING_HORIZONS = [
    ("1 month", 30),
    ("3 months", 91),
    ("6 months", 182),
    ("1 year", 365),
    ("5 years", 365 * 5),
    ("10 years", 365 * 10),
    ("20 years", 365 * 20),
]


# ---------------------------------------------------------------- 11.1 Dashboard

def get_fund_metadata(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fund_metadata WHERE id = 1")
        return cur.fetchone()


def get_latest_portfolio_snapshot(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        )
        return cur.fetchone()


def get_pipeline_stage_status(conn):
    """Most recent job_runs row per known stage, for the Dashboard's
    pipeline-chain visualization and status row. A stage with no rows at
    all means its phase hasn't been built yet — 'idle', not 'failed'.

    Excludes triggered_by='backfill' rows: Phase 3's retroactive backfill
    (scripts/backfill_factor_scores.py) logs under stage='factor_scoring',
    the same stage real weekly cron uses. Backfill runs happen with
    today's wall-clock timestamps, so without this filter a multi-day
    backfill would become the 'most recent' factor_scoring row and this
    screen would silently stop reflecting real cron health — exactly the
    signal DESIGN.md's full-week-of-cron checkpoint depends on."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (stage) stage, run_type, triggered_by,
                   start_time, end_time, status, error_message
            FROM job_runs
            WHERE triggered_by != 'backfill'
            ORDER BY stage, start_time DESC
            """
        )
        by_stage = {row["stage"]: row for row in cur.fetchall()}

    results = []
    for stage_key, label in PIPELINE_STAGES:
        row = by_stage.get(stage_key)
        if row is None:
            results.append({"stage": stage_key, "label": label, "status": "idle", "row": None})
        else:
            results.append({"stage": stage_key, "label": label, "status": row["status"], "row": row})
    return results


def get_todays_pick(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM daily_picks
            WHERE trade_date = (SELECT MAX(trade_date) FROM daily_picks)
            ORDER BY dollar_allocated DESC NULLS LAST
            """
        )
        return cur.fetchall()


def get_latest_portfolio_risk_snapshot(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM portfolio_risk_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        )
        return cur.fetchone()


# ---------------------------------------------------------------- 11.2 Returns

def get_trailing_returns(conn):
    """Portfolio vs S&P 500 return over each trailing horizon. A horizon
    beyond the fund's actual track record renders 'Insufficient history'
    rather than a fabricated number (DESIGN.md §11.2) — determined here by
    checking whether a snapshot actually exists on/before that horizon's
    start date, not just by whether the query returns a row."""
    with conn.cursor() as cur:
        cur.execute("SELECT MIN(snapshot_date) AS earliest FROM portfolio_snapshots")
        earliest_row = cur.fetchone()
        earliest = earliest_row["earliest"] if earliest_row else None

        cur.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        )
        latest = cur.fetchone()

    results = []
    today = date.today()
    for label, days in TRAILING_HORIZONS:
        horizon_start = today - timedelta(days=days)
        has_history = earliest is not None and earliest <= horizon_start
        results.append({
            "label": label,
            "has_history": has_history,
            "portfolio_return_pct": latest["cumulative_return_pct"] if (has_history and latest) else None,
            # Benchmark comparison needs its own S&P 500 series, not yet
            # ingested anywhere in this schema — left None deliberately
            # rather than silently omitted, so the template can say so.
            "benchmark_return_pct": None,
        })
    return results


# ---------------------------------------------------------------- 11.3 Universe & Ingestion

def get_ingestion_progress(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ic.data_type, ic.daily_budget,
                   COUNT(*) FILTER (WHERE ist.fetch_status = 'complete') AS complete_count,
                   COUNT(*) AS total_count
            FROM ingestion_config ic
            LEFT JOIN ingestion_state ist ON ist.data_type = ic.data_type
            GROUP BY ic.data_type, ic.daily_budget
            ORDER BY ic.data_type
            """
        )
        rows = cur.fetchall()

    results = []
    for row in rows:
        remaining = max(row["total_count"] - row["complete_count"], 0)
        days_remaining = None
        if row["daily_budget"] and row["daily_budget"] > 0:
            days_remaining = -(-remaining // row["daily_budget"])  # ceiling division
        results.append({**row, "remaining": remaining, "days_remaining": days_remaining})
    return results


def get_universe(conn, sector=None, active_only=True):
    query = "SELECT * FROM universe WHERE TRUE"
    params = []
    if active_only:
        query += " AND is_active = TRUE"
    if sector:
        query += " AND sector = %s"
        params.append(sector)
    query += " ORDER BY market_cap DESC NULLS LAST LIMIT 500"
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_universe_sectors(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT sector FROM universe WHERE sector IS NOT NULL ORDER BY sector"
        )
        return [row["sector"] for row in cur.fetchall()]


def get_recent_universe_changes(conn, limit=25):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM universe_changes ORDER BY change_date DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()


def add_manual_ticker(conn, ticker, company_name, sector):
    ticker = ticker.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO universe (ticker, company_name, sector, source, is_active)
            VALUES (%s, %s, %s, 'manual', TRUE)
            ON CONFLICT (ticker) DO UPDATE SET
                company_name = EXCLUDED.company_name,
                sector = EXCLUDED.sector,
                is_active = TRUE
            """,
            (ticker, company_name or None, sector or None),
        )
        cur.execute(
            """
            INSERT INTO universe_changes (ticker, change_type, change_date, source_index, detected_by, reason)
            VALUES (%s, 'added', CURRENT_DATE, 'manual', 'manual', 'Added via GUI')
            """,
            (ticker,),
        )
    conn.commit()


def prioritize_ticker(conn, ticker, data_type):
    """Bumps a ticker to the front of the next ingestion cron run. This is
    a cheap, instant metadata write only — it does NOT trigger a fetch
    itself. The actual fetch still happens on the next scheduled cron run,
    same as every other ticker; this just changes its place in line."""
    ticker = ticker.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingestion_state SET priority_rank = 0
            WHERE ticker = %s AND data_type = %s
            """,
            (ticker, data_type),
        )
    conn.commit()


def force_refresh_ticker(conn, ticker, data_type):
    """Resets a ticker back to 'pending' so the next cron run re-fetches
    it, same mechanism used to reset stale post-bugfix data during Phase 1
    (see conversation history: the ingestion_state.fetch_status reset used
    to force re-fetching fundamentals after the exact-date-match fix)."""
    ticker = ticker.strip().upper()
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ingestion_state SET fetch_status = 'pending', retry_count = 0, priority_rank = 0
            WHERE ticker = %s AND data_type = %s
            """,
            (ticker, data_type),
        )
    conn.commit()


# ---------------------------------------------------------------- 11.4 Pipeline Runner

def get_pipeline_runs(conn, stage=None, run_type=None, limit=50):
    query = "SELECT * FROM job_runs WHERE TRUE"
    params = []
    if stage:
        query += " AND stage = %s"
        params.append(stage)
    if run_type:
        query += " AND run_type = %s"
        params.append(run_type)
    query += " ORDER BY start_time DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_latest_factor_scores(conn, limit=25):
    # CONFIRMED (2026-07-14): factor_scores' PRIMARY KEY is (ticker,
    # score_date, run_id) — INTENTIONALLY keeps every run as its own row
    # for audit history, not overwritten. But run_id is a random UUID
    # (job_run.py: f"{stage}-{uuid4().hex[:10]}"), NOT chronologically
    # sortable — so a plain `WHERE score_date = MAX(score_date)` returns
    # EVERY run from today mixed together, with no way to tell which row
    # per ticker is actually the latest. On a day with several manual
    # re-runs (common while iterating), this silently mixes stale and
    # fresh values for the same ticker with no indication anything is
    # wrong. Fixed by joining to job_runs (which DOES have start_time)
    # and keeping only the most-recent run per ticker via ROW_NUMBER().
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH ranked AS (
                SELECT fs.ticker, fs.score_date, fs.momentum_z, fs.quality_z,
                       fs.value_z, fs.lowvol_z, fs.decile_rank,
                       ROW_NUMBER() OVER (
                           PARTITION BY fs.ticker ORDER BY jr.start_time DESC
                       ) AS rn
                FROM factor_scores fs
                JOIN job_runs jr ON jr.run_id = fs.run_id
                WHERE fs.score_date = (SELECT MAX(score_date) FROM factor_scores)
            )
            SELECT ticker, score_date, momentum_z, quality_z, value_z, lowvol_z, decile_rank
            FROM ranked
            WHERE rn = 1
            ORDER BY momentum_z DESC NULLS LAST
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------- 11.5 Positions & Trades

def get_positions(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM positions ORDER BY market_value DESC NULLS LAST")
        return cur.fetchall()


def get_trades(conn, sleeve=None, side=None, limit=100):
    query = "SELECT * FROM trades WHERE TRUE"
    params = []
    if sleeve:
        query += " AND sleeve = %s"
        params.append(sleeve)
    if side:
        query += " AND side = %s"
        params.append(side)
    query += " ORDER BY trade_date DESC LIMIT %s"
    params.append(limit)
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_signal_vs_execution(conn, limit=50):
    """daily_picks alongside trades for the same ticker/date, surfacing
    slippage between what the model recommended and what was executed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT dp.ticker, dp.trade_date, dp.dollar_allocated AS signaled_dollars,
                   dp.executed, t.dollar_amount AS executed_dollars, t.price AS executed_price
            FROM daily_picks dp
            LEFT JOIN trades t ON t.ticker = dp.ticker AND t.trade_date = dp.trade_date
            ORDER BY dp.trade_date DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------- 11.6 Exit Signals

def get_exit_signals(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM exit_signals ORDER BY signal_date DESC, unrealized_pnl_pct ASC"
        )
        return cur.fetchall()


def get_exit_rules_config(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM exit_rules_config ORDER BY rule_type")
        return cur.fetchall()


# ---------------------------------------------------------------- 11.7 Hedge Sleeve

def get_latest_hedge_state(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM vol_hedge_state ORDER BY date DESC LIMIT 1")
        return cur.fetchone()


def get_hedge_history(conn, limit=90):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT date, implied_vol, realized_vol, garch_forecast, term_structure_signal
            FROM vol_hedge_state ORDER BY date DESC LIMIT %s
            """,
            (limit,),
        )
        return list(reversed(cur.fetchall()))


# ---------------------------------------------------------------- 11.8 Portfolio Risk

def get_portfolio_risk_history(conn, limit=90):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT snapshot_date, portfolio_vol, portfolio_beta, sector_concentration, factor_exposure
            FROM portfolio_risk_snapshots ORDER BY snapshot_date DESC LIMIT %s
            """,
            (limit,),
        )
        return list(reversed(cur.fetchall()))


# ---------------------------------------------------------------- 11.9 Backtest Runner

def get_backtest_runs(conn, limit=25):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM backtest_runs ORDER BY started_at DESC NULLS LAST LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def get_backtest_segments(conn, backtest_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM backtest_segments WHERE backtest_id = %s ORDER BY segment_index",
            (backtest_id,),
        )
        return cur.fetchall()


def get_backtest_experiment_log(conn, limit=25):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM backtest_experiment_log ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def queue_backtest_run(conn, run_name, start_date, end_date, config_snapshot, is_holdout_run=False):
    """Writes queued backtest_runs + backtest_segments rows (one per
    calendar year in range), per DESIGN.md §11.9. Deliberately does NOT
    execute anything — Phase 10's actual backtest engine (year-segmented
    GitHub Actions runs) doesn't exist yet, so this queues real rows an
    eventual worker can pick up, rather than faking execution."""
    import uuid

    backtest_id = f"bt-{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_runs (backtest_id, run_name, start_date, end_date,
                                        config_snapshot, is_holdout_run, status, progress_pct)
            VALUES (%s, %s, %s, %s, %s, %s, 'queued', 0)
            """,
            (backtest_id, run_name, start_date, end_date, config_snapshot, is_holdout_run),
        )
        year = start_date.year
        idx = 0
        while year <= end_date.year:
            seg_start = date(year, 1, 1) if year > start_date.year else start_date
            seg_end = date(year, 12, 31) if year < end_date.year else end_date
            cur.execute(
                """
                INSERT INTO backtest_segments (segment_id, backtest_id, segment_index,
                                                segment_start_date, segment_end_date, status)
                VALUES (%s, %s, %s, %s, %s, 'queued')
                """,
                (f"{backtest_id}-seg{idx}", backtest_id, idx, seg_start, seg_end),
            )
            year += 1
            idx += 1
    conn.commit()
    return backtest_id
