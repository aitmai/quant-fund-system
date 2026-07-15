"""
GUI smoke tests. Mocks every src.gui.queries function so these run without
a real Postgres connection — verifies routing, Jinja templates, and
url_for references all resolve, not the SQL itself (that's exercised by
manual testing against real Supabase, same as every other script here).
"""

import os
import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql://fake:fake@localhost/fake")

FIXTURES = {
    "get_fund_metadata": {"fund_start_date": date(2026, 6, 1), "initial_capital": 1_000_000, "total_cash_invested": 1_000_000},
    "get_latest_portfolio_snapshot": {"total_nav": 1_015_000, "long_sleeve_value": 900_000, "hedge_sleeve_value": 100_000, "cash_balance": 15_000, "daily_pnl": 1_500, "daily_return_pct": 0.15, "cumulative_return_pct": 1.5},
    "get_pipeline_stage_status": [
        {"stage": "universe_sync", "label": "Stage 1 · Universe Sync", "status": "complete",
         "row": {"start_time": datetime(2026, 7, 14, 13, 0), "triggered_by": "cron", "status": "complete", "error_message": None}},
        {"stage": "ingestion_price", "label": "Stage 1 · Price Ingestion", "status": "running",
         "row": {"start_time": datetime(2026, 7, 14, 14, 17), "triggered_by": "cron", "status": "running", "error_message": None}},
        {"stage": "factor_scoring", "label": "Stage 2 · Factor Scoring", "status": "failed",
         "row": {"start_time": datetime(2026, 7, 14, 9, 0), "triggered_by": "aitmai", "status": "failed", "error_message": "boom"}},
        {"stage": "ml_ranking", "label": "Stage 3 · ML Ranking", "status": "idle", "row": None},
    ],
    "get_todays_pick": [{"ticker": "AAPL", "dollar_allocated": 5000, "executed": False}],
    "get_latest_portfolio_risk_snapshot": {"portfolio_vol": 12.5, "portfolio_beta": 0.9, "sector_concentration": {"Technology": 22.0}, "factor_exposure": {"momentum": 0.4}},
    "get_trailing_returns": [
        {"label": "1 month", "has_history": True, "portfolio_return_pct": 1.2, "benchmark_return_pct": None},
        {"label": "5 years", "has_history": False, "portfolio_return_pct": None, "benchmark_return_pct": None},
    ],
    "get_ingestion_progress": [
        {"data_type": "price", "daily_budget": 400, "complete_count": 49, "total_count": 503, "remaining": 454, "days_remaining": 2},
    ],
    "get_universe": [{"ticker": "AAPL", "company_name": "Apple Inc.", "sector": "Technology", "market_cap": 3_000_000_000_000, "source": "auto"}],
    "get_universe_sectors": ["Technology"],
    "get_recent_universe_changes": [{"change_date": date(2026, 7, 10), "ticker": "AAPL", "change_type": "added", "source_index": "sp500", "detected_by": "cron"}],
    "get_pipeline_runs": [{"run_id": "factor_scoring-abc123", "stage": "factor_scoring", "run_type": "manual", "status": "complete", "start_time": datetime(2026, 7, 14, 9, 0), "error_message": None}],
    "get_latest_factor_scores": [{"ticker": "AAPL", "score_date": date(2026, 7, 14), "momentum_z": 1.5, "quality_z": 0.8, "value_z": -0.3, "lowvol_z": 0.1}],
    "get_positions": [],
    "get_trades": [],
    "get_signal_vs_execution": [],
    "get_exit_signals": [],
    "get_exit_rules_config": [{"rule_type": "min_hold", "threshold_value": 4, "lookback_period": "quarters"}],
    "get_latest_hedge_state": None,
    "get_hedge_history": [],
    "get_portfolio_risk_history": [],
    "get_backtest_runs": [],
    "get_backtest_experiment_log": [],
}


def _make_client():
    """Patches get_db_connection + every queries function, returns a test client."""
    patcher = patch("src.gui.routes.get_db_connection", return_value=MagicMock())
    patcher.start()

    import src.gui.queries as q
    for name, val in FIXTURES.items():
        setattr(q, name, (lambda v: (lambda conn, *a, **kw: v))(val))

    from src.gui import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client(), patcher


class TestGuiRoutesRender(unittest.TestCase):
    def setUp(self):
        self.client, self.patcher = _make_client()

    def tearDown(self):
        self.patcher.stop()

    def test_all_get_routes_render_200(self):
        routes = [
            "/", "/returns", "/universe", "/universe?sector=Technology", "/pipeline",
            "/positions", "/positions?sleeve=long", "/exit-signals", "/hedge",
            "/portfolio-risk", "/backtest",
        ]
        for route in routes:
            with self.subTest(route=route):
                resp = self.client.get(route)
                self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True)[:500])

    def test_nav_bar_present_on_every_screen(self):
        """DESIGN.md §11: 'No screen should render a partial nav; all eight
        tabs are always present regardless of which page is active.'"""
        tabs = ["Dashboard", "Returns", "Universe", "Pipeline", "Positions", "Exit signals", "Hedge", "Backtest"]
        for route in ["/", "/universe", "/backtest"]:
            body = self.client.get(route).get_data(as_text=True)
            for tab in tabs:
                with self.subTest(route=route, tab=tab):
                    self.assertIn(tab, body)

    def test_insufficient_history_shown_not_fabricated(self):
        body = self.client.get("/returns").get_data(as_text=True)
        self.assertIn("Insufficient history", body)

    def test_zero_daily_budget_does_not_render_literal_none(self):
        # CONFIRMED (2026-07-14): queries.get_ingestion_progress() returns
        # days_remaining=None when daily_budget is 0/unset (ceiling-division
        # guard). The template had no None-guard, so it would render the
        # literal text "None day(s) left" on screen for that data_type.
        import src.gui.queries as q
        zero_budget_progress = [
            {"data_type": "fundamentals", "daily_budget": 0, "complete_count": 0,
             "total_count": 503, "remaining": 503, "days_remaining": None},
        ]
        original = q.get_ingestion_progress
        q.get_ingestion_progress = lambda conn, *a, **kw: zero_budget_progress
        try:
            body = self.client.get("/universe").get_data(as_text=True)
            self.assertNotIn("None day(s)", body)
            self.assertIn("no daily budget configured", body)
        finally:
            q.get_ingestion_progress = original


class TestGuiWriteActions(unittest.TestCase):
    def setUp(self):
        self.client, self.patcher = _make_client()
        import src.gui.queries as q
        self.add_calls = []
        q.add_manual_ticker = lambda *a, **kw: self.add_calls.append((a, kw))
        q.prioritize_ticker = lambda *a, **kw: None
        q.force_refresh_ticker = lambda *a, **kw: None
        q.queue_backtest_run = lambda *a, **kw: "bt-testid1234"

        import src.scoring.run_factor_scoring as rfs
        self._orig_run = rfs.run
        rfs.run = lambda conn, job=None, score_date=None: {"total_active": 503}

        import src.job_run as jr
        self._orig_enter, self._orig_exit = jr.JobRun.__enter__, jr.JobRun.__exit__
        jr.JobRun.__enter__ = lambda self: self
        jr.JobRun.__exit__ = lambda self, *a: False

    def tearDown(self):
        self.patcher.stop()
        import src.scoring.run_factor_scoring as rfs
        rfs.run = self._orig_run
        import src.job_run as jr
        jr.JobRun.__enter__, jr.JobRun.__exit__ = self._orig_enter, self._orig_exit

    def test_add_manual_ticker(self):
        resp = self.client.post("/universe/add", data={"ticker": "TSLA", "sector": "Consumer"}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.add_calls), 1)

    def test_add_manual_ticker_blank_rejected_without_db_call(self):
        resp = self.client.post("/universe/add", data={"ticker": "  "}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.add_calls), 0)

    def test_run_factor_scoring_now(self):
        resp = self.client.post("/pipeline/run-factor-scoring", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def test_queue_backtest_valid_dates(self):
        resp = self.client.post(
            "/backtest/queue",
            data={"run_name": "test", "start_date": "2020-01-01", "end_date": "2022-12-31"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Queued bt-testid1234", resp.data)

    def test_queue_backtest_invalid_date_shows_error_not_500(self):
        resp = self.client.post(
            "/backtest/queue",
            data={"start_date": "not-a-date", "end_date": "2022-12-31"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"valid start and end dates", resp.data)


class TestGuiDatabaseUnavailable(unittest.TestCase):
    def test_missing_database_url_renders_graceful_503(self):
        env_backup = os.environ.pop("DATABASE_URL", None)
        try:
            from src.gui import create_app
            app = create_app()
            app.config["TESTING"] = True
            resp = app.test_client().get("/")
            self.assertEqual(resp.status_code, 503)
            self.assertIn(b"reach the database", resp.data)
        finally:
            if env_backup is not None:
                os.environ["DATABASE_URL"] = env_backup


if __name__ == "__main__":
    unittest.main()
