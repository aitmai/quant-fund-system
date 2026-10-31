"""Offline deterministic regression tests for point-in-time universe reconstruction."""
import sqlite3
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.scoring import data_fetch, run_factor_scoring


class CursorAdapter:
    def __init__(self, db):
        self.db = db
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=()):
        params = tuple(
            value.isoformat() if isinstance(value, date) else value
            for value in params
        )
        self.result = self.db.execute(sql.replace("%s", "?"), params)

    def fetchall(self):
        return self.result.fetchall()


class ConnectionAdapter:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.executescript("""
            CREATE TABLE universe (ticker TEXT PRIMARY KEY, sector TEXT, is_active BOOLEAN, added_date DATE DEFAULT '2024-01-01');
            CREATE TABLE universe_changes (
                id INTEGER PRIMARY KEY, ticker TEXT, change_type TEXT, change_date DATE
            );
        """)

    def cursor(self):
        return CursorAdapter(self.db)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()


class HistoricalUniverseTests(unittest.TestCase):
    def setUp(self):
        self.conn = ConnectionAdapter()
        self.conn.db.executemany(
            "INSERT INTO universe (ticker, sector, is_active) VALUES (?, ?, ?)",
            [("A", "Tech", False), ("B", "Tech", True), ("C", None, True)],
        )
        self.conn.db.executemany(
            "INSERT INTO universe_changes VALUES (?, ?, ?, ?)",
            [
                (1, "A", "added", "2024-01-01"),
                (2, "B", "added", "2024-01-01"),
                (3, "C", "added", "2025-01-01"),
                (4, "B", "removed", "2025-06-01"),
                (5, "B", "added", "2026-02-01"),
                (6, "A", "removed", "2026-03-01"),
            ],
        )

    def names(self, as_of=None):
        frame = data_fetch.fetch_active_universe(self.conn, as_of=as_of)
        return set(frame["ticker"])

    def test_unchanged_member_without_addition_event(self):
        # D was already a member when event logging began.
        # No transition occurred, so no event was recorded.
        self.conn.db.execute(
            "INSERT INTO universe (ticker, sector, is_active) VALUES (?, ?, ?)",
            ("D", "Tech", True),
        )

        self.assertEqual(
            self.names(date(2025, 2, 1)),
            {"A", "B", "C", "D"},
        )

    def test_unlogged_later_addition_is_excluded(self):
        self.conn.db.execute(
            "INSERT INTO universe "
            "(ticker, sector, is_active, added_date) "
            "VALUES (?, ?, ?, ?)",
            ("LATE", "Tech", True, "2026-01-01"),
        )

        self.assertNotIn("LATE", self.names(date(2025, 2, 1)))

    def test_future_removal_respects_initial_added_date(self):
        self.conn.db.execute(
            "INSERT INTO universe "
            "(ticker, sector, is_active, added_date) "
            "VALUES (?, ?, ?, ?)",
            ("LATE", "Tech", False, "2025-06-01"),
        )
        self.conn.db.execute(
            "INSERT INTO universe_changes VALUES (?, ?, ?, ?)",
            (7, "LATE", "removed", "2026-01-01"),
        )

        self.assertNotIn("LATE", self.names(date(2025, 2, 1)))
        self.assertIn("LATE", self.names(date(2025, 8, 1)))

    def test_current_live_behavior_remains_unchanged(self):
        self.assertEqual(self.names(), {"B", "C"})

    def test_historical_replay_excludes_future_additions_and_includes_delisted_names(self):
        self.assertEqual(self.names(date(2024, 6, 1)), {"A", "B"})
        self.assertEqual(self.names(date(2025, 8, 1)), {"A", "C"})

    def test_removal_then_reentry(self):
        self.assertEqual(self.names(date(2026, 2, 10)), {"A", "B", "C"})
        self.assertEqual(self.names(date(2026, 4, 1)), {"B", "C"})

    def test_future_changes_do_not_change_past_universe(self):
        before = self.names(date(2024, 6, 1))
        self.conn.db.execute(
            "INSERT INTO universe_changes VALUES (?, ?, ?, ?)",
            (7, "B", "removed", "2026-12-01"),
        )
        self.assertEqual(self.names(date(2024, 6, 1)), before)

    def test_same_day_latest_event_wins(self):
        self.conn.db.execute(
            "INSERT INTO universe_changes VALUES (?, ?, ?, ?)",
            (7, "B", "removed", "2026-02-01"),
        )
        self.assertEqual(self.names(date(2026, 2, 2)), {"A", "C"})

    def test_before_log_coverage_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "No recorded universe membership"):
            self.names(date(2023, 1, 1))

    def test_unknown_sector_is_preserved_as_unknown(self):
        df = data_fetch.fetch_active_universe(self.conn, as_of=date(2025, 2, 1))
        self.assertEqual(df.set_index("ticker").loc["C", "sector"], "Unknown")


class ScoringIntegrationTests(unittest.TestCase):
    def test_historical_date_passed_through_and_nonmembers_filtered(self):
        as_of = date(2025, 5, 1)
        universe = pd.DataFrame({"ticker": ["A", "B"], "sector": ["Tech", "Tech"]})
        prices = pd.DataFrame({"ticker": ["A", "B", "FUTURE"], "price": [1., 2., 100.]})
        fundamentals = pd.DataFrame({"ticker": ["A", "B", "FUTURE"], "roe": [1., 2., 900.]})
        captured = {}

        def factor_fn(frame):
            captured["price_tickers"] = set(frame["ticker"])
            return {}, {}, set(), set()

        def fundamental_fn(frame):
            captured["fundamental_tickers"] = set(frame["ticker"])
            return ({"roe": {}, "debt_equity_inv": {}, "earnings_stability": {}},
                    {"fcf_yield": {}, "ev_ebitda_inv": {}})

        with patch.object(data_fetch, "fetch_active_universe", return_value=universe) as get_universe, \
             patch.object(data_fetch, "fetch_price_panel", return_value=prices), \
             patch.object(data_fetch, "fetch_latest_fundamentals", return_value=fundamentals), \
             patch.object(run_factor_scoring, "compute_momentum_and_lowvol", side_effect=factor_fn), \
             patch.object(run_factor_scoring, "compute_quality_value_raw", side_effect=fundamental_fn), \
             patch.object(run_factor_scoring, "_upsert_factor_scores"):
            run_factor_scoring.run(MagicMock(), score_date=as_of)

        get_universe.assert_called_once_with(unittest.mock.ANY, as_of=as_of)
        self.assertEqual(captured["price_tickers"], {"A", "B"})
        self.assertEqual(captured["fundamental_tickers"], {"A", "B"})


if __name__ == "__main__":
    unittest.main()
