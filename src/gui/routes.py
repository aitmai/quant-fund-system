"""
GUI routes — one view function per screen in DESIGN.md §11's numbering.

Connection lifecycle: one psycopg2 connection per request, opened lazily
via Flask's `g` and closed in teardown_appcontext — the standard Flask
pattern, so every route just calls get_db() without thinking about
connect/close itself.

Write actions are deliberately limited to what's safely supported today:
  - Universe: manual ticker add, force-refresh/prioritize (cheap metadata
    writes only — they influence what the next cron run picks up first,
    they do NOT trigger a fetch inline).
  - Pipeline Runner: "Run now" for factor scoring only, since it's the
    one stage fast enough to run synchronously inside an HTTP request
    without hitting Render's request timeout or racing the hourly
    ingestion cron over shared budget/state.
  - Backtest Runner: "Run backtest" queues real backtest_runs +
    backtest_segments rows; it does not execute anything, since Phase 10's
    actual backtest engine doesn't exist yet.
Every other button in the wireframe (Approve exit, Hedge actions, Pipeline
config-panel edits) renders per spec but is marked "not yet enabled" in
the template rather than silently doing nothing — see each template's
`{% if not ... %}` disabled-state blocks.
"""

from datetime import date, datetime

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from src.gui import queries
from src.universe.construct_universe import parse_bare_ticker_list, upload_manual_tickers
from src.gui.db import DatabaseUnavailable, get_db_connection
from src.job_run import JobRun

gui_bp = Blueprint("gui", __name__, template_folder="templates", static_folder="static")

NAV_ITEMS = [
    ("gui.dashboard", "Dashboard"),
    ("gui.returns", "Returns"),
    ("gui.universe", "Universe"),
    ("gui.pipeline", "Pipeline"),
    ("gui.positions", "Positions"),
    ("gui.exit_signals", "Exit signals"),
    ("gui.hedge", "Hedge"),
    ("gui.portfolio_risk", "Portfolio risk"),
    ("gui.backtest", "Backtest"),
]


def get_db():
    if "db" not in g:
        g.db = get_db_connection()
    return g.db


@gui_bp.teardown_app_request
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@gui_bp.app_errorhandler(DatabaseUnavailable)
def handle_db_unavailable(error):
    return render_template("error.html", nav_items=NAV_ITEMS, message=str(error)), 503


# ---------------------------------------------------------------- 11.1 Dashboard

@gui_bp.route("/")
def dashboard():
    conn = get_db()
    return render_template(
        "dashboard.html",
        nav_items=NAV_ITEMS,
        fund=queries.get_fund_metadata(conn),
        snapshot=queries.get_latest_portfolio_snapshot(conn),
        stages=queries.get_pipeline_stage_status(conn),
        picks=queries.get_todays_pick(conn),
        risk=queries.get_latest_portfolio_risk_snapshot(conn),
    )


# ---------------------------------------------------------------- 11.2 Returns

@gui_bp.route("/returns")
def returns():
    conn = get_db()
    return render_template(
        "returns.html", nav_items=NAV_ITEMS, rows=queries.get_trailing_returns(conn)
    )


# ---------------------------------------------------------------- 11.3 Universe & Ingestion

@gui_bp.route("/universe")
def universe():
    conn = get_db()
    sector = request.args.get("sector") or None
    return render_template(
        "universe.html",
        nav_items=NAV_ITEMS,
        progress=queries.get_ingestion_progress(conn),
        tickers=queries.get_universe(conn, sector=sector),
        sectors=queries.get_universe_sectors(conn),
        selected_sector=sector,
        changes=queries.get_recent_universe_changes(conn),
    )


@gui_bp.route("/universe/add", methods=["POST"])
def universe_add():
    conn = get_db()
    ticker = request.form.get("ticker", "")
    if not ticker.strip():
        flash("Enter a ticker symbol.", "error")
        return redirect(url_for("gui.universe"))
    queries.add_manual_ticker(
        conn, ticker, request.form.get("company_name"), request.form.get("sector")
    )
    flash(f"Added {ticker.strip().upper()} to the universe.", "success")
    return redirect(url_for("gui.universe"))


@gui_bp.route("/universe/bulk-upload", methods=["POST"])
def universe_bulk_upload():
    """Bulk ticker upload from a plain .txt file — one ticker per line
    and/or comma-separated, both accepted (parse_bare_ticker_list
    handles either). Deliberately does NOT do inline sector/company-name
    enrichment here (that would mean a synchronous yfinance call per
    ticker inside an HTTP request — slow, and risks Render's request
    timeout on a large list). Tickers land with is_active=TRUE,
    source='manual', sector/company_name left NULL — the same "cheap
    metadata write only, no fetch triggered inline" rule this file's
    module docstring already establishes for the single-ticker add
    route. Enriching sector afterward is a deliberate separate CLI step
    (scripts/lookup_ticker_sectors.py -> scripts/upload_manual_tickers.py),
    not squeezed into this request."""
    conn = get_db()
    uploaded_file = request.files.get("ticker_file")
    if not uploaded_file or not uploaded_file.filename:
        flash("Choose a .txt file of tickers to upload.", "error")
        return redirect(url_for("gui.universe"))

    try:
        text = uploaded_file.read().decode("utf-8")
    except UnicodeDecodeError:
        flash("Could not read that file as text — make sure it's a plain .txt file.", "error")
        return redirect(url_for("gui.universe"))

    tickers = parse_bare_ticker_list(text)
    if not tickers:
        flash("No tickers found in that file.", "error")
        return redirect(url_for("gui.universe"))

    result = upload_manual_tickers(conn, [{"ticker": t} for t in tickers], run_type="manual", triggered_by="gui")
    flash(
        f"Uploaded {result['added']} ticker(s) from {uploaded_file.filename}. "
        f"Sector/company name left blank — run scripts/lookup_ticker_sectors.py "
        f"to enrich them.",
        "success",
    )
    return redirect(url_for("gui.universe"))


@gui_bp.route("/universe/prioritize", methods=["POST"])
def universe_prioritize():
    conn = get_db()
    queries.prioritize_ticker(conn, request.form["ticker"], request.form["data_type"])
    flash(f"{request.form['ticker'].upper()} moved to the front of the next {request.form['data_type']} run.", "success")
    return redirect(url_for("gui.universe"))


@gui_bp.route("/universe/force-refresh", methods=["POST"])
def universe_force_refresh():
    conn = get_db()
    queries.force_refresh_ticker(conn, request.form["ticker"], request.form["data_type"])
    flash(f"{request.form['ticker'].upper()} reset to pending — will re-fetch next cron run.", "success")
    return redirect(url_for("gui.universe"))


# ---------------------------------------------------------------- 11.4 Pipeline Runner

@gui_bp.route("/pipeline")
def pipeline():
    conn = get_db()
    return render_template(
        "pipeline.html",
        nav_items=NAV_ITEMS,
        stages=queries.get_pipeline_stage_status(conn),
        runs=queries.get_pipeline_runs(conn, limit=30),
        latest_scores=queries.get_latest_factor_scores(conn),
    )


@gui_bp.route("/pipeline/run-factor-scoring", methods=["POST"])
def pipeline_run_factor_scoring():
    """The one stage safe to run synchronously inside a web request — see
    module docstring for why the others aren't wired this way yet."""
    conn = get_db()
    from src.scoring import run_factor_scoring as run_factor_scoring_module

    try:
        with JobRun(conn, stage="factor_scoring", run_type="manual", triggered_by="gui") as job:
            summary = run_factor_scoring_module.run(conn, job=job)
        flash(f"Factor scoring complete: {summary}", "success")
    except Exception as exc:
        flash(f"Factor scoring failed: {exc}", "error")
    return redirect(url_for("gui.pipeline"))


# ---------------------------------------------------------------- 11.5 Positions & Trades

@gui_bp.route("/positions")
def positions():
    conn = get_db()
    sleeve = request.args.get("sleeve") or None
    side = request.args.get("side") or None
    return render_template(
        "positions.html",
        nav_items=NAV_ITEMS,
        upcoming_picks=queries.get_upcoming_picks(conn),
        positions=queries.get_positions(conn),
        trades=queries.get_trades(conn, sleeve=sleeve, side=side),
        signal_vs_execution=queries.get_signal_vs_execution(conn),
        selected_sleeve=sleeve,
        selected_side=side,
    )


# ---------------------------------------------------------------- 11.6 Exit Signals

@gui_bp.route("/exit-signals")
def exit_signals():
    conn = get_db()
    return render_template(
        "exit_signals.html",
        nav_items=NAV_ITEMS,
        signals=queries.get_exit_signals(conn),
        rules=queries.get_exit_rules_config(conn),
    )


# ---------------------------------------------------------------- 11.7 Hedge Sleeve

@gui_bp.route("/hedge")
def hedge():
    conn = get_db()
    return render_template(
        "hedge.html",
        nav_items=NAV_ITEMS,
        latest=queries.get_latest_hedge_state(conn),
        history=queries.get_hedge_history(conn),
    )


# ---------------------------------------------------------------- 11.8 Portfolio Risk

@gui_bp.route("/portfolio-risk")
def portfolio_risk():
    conn = get_db()
    return render_template(
        "portfolio_risk.html",
        nav_items=NAV_ITEMS,
        history=queries.get_portfolio_risk_history(conn),
        latest=queries.get_latest_portfolio_risk_snapshot(conn),
    )


# ---------------------------------------------------------------- 11.9 Backtest Runner

@gui_bp.route("/backtest")
def backtest():
    conn = get_db()
    return render_template(
        "backtest.html",
        nav_items=NAV_ITEMS,
        runs=queries.get_backtest_runs(conn),
        experiment_log=queries.get_backtest_experiment_log(conn),
    )


@gui_bp.route("/backtest/queue", methods=["POST"])
def backtest_queue():
    conn = get_db()
    try:
        start = date.fromisoformat(request.form["start_date"])
        end = date.fromisoformat(request.form["end_date"])
    except (KeyError, ValueError):
        flash("Enter valid start and end dates.", "error")
        return redirect(url_for("gui.backtest"))

    config_snapshot = {
        "correlation_window": request.form.get("correlation_window"),
        "exclusion_threshold": request.form.get("exclusion_threshold"),
        "target_vol": request.form.get("target_vol"),
        "sector_cap": request.form.get("sector_cap"),
        "stop_loss_threshold": request.form.get("stop_loss_threshold"),
        "transaction_cost_bps": request.form.get("transaction_cost_bps"),
    }
    is_holdout = request.form.get("is_holdout_run") == "on"

    import json
    backtest_id = queries.queue_backtest_run(
        conn,
        run_name=request.form.get("run_name") or f"backtest-{datetime.now():%Y%m%d-%H%M}",
        start_date=start,
        end_date=end,
        config_snapshot=json.dumps(config_snapshot),
        is_holdout_run=is_holdout,
    )
    flash(
        f"Queued {backtest_id} ({start} \u2192 {end}). No execution engine is running yet "
        f"(Phase 10) — this reserves the run for when it is.",
        "success",
    )
    return redirect(url_for("gui.backtest"))
