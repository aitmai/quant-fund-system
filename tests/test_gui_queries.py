"""
Tests for src/gui/queries.py SQL correctness — as opposed to test_gui.py,
which mocks every queries function away entirely to test routing/templates.
These verify the actual SQL text/params sent to the database for logic
that's easy to get subtly wrong (like deduplication), since a wrong
result here wouldn't show up as a crash — it would just silently return
the wrong (but plausible-looking) row.
"""

import unittest
from unittest.mock import MagicMock

from src.gui import queries


class TestGetLatestFactorScores(unittest.TestCase):
    """CONFIRMED (2026-07-14): factor_scores' PRIMARY KEY is (ticker,
    score_date, run_id) — multiple manual re-runs on the same score_date
    (common while iterating on Phase 2) create multiple rows per ticker.
    run_id is a random UUID, not chronologically sortable, so the OLD
    query (`WHERE score_date = MAX(score_date)`, no further dedup) could
    return several rows for the same ticker mixed with stale data from
    earlier same-day runs. Verifies the fix: joins to job_runs (which has
    start_time) and keeps only the most-recent run per ticker."""

    def _fake_conn(self, rows=None):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows or []
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_query_deduplicates_by_latest_run_per_ticker(self):
        conn, cursor = self._fake_conn()
        queries.get_latest_factor_scores(conn, limit=25)

        executed_sql = cursor.execute.call_args[0][0]
        executed_params = cursor.execute.call_args[0][1]

        self.assertIn("ROW_NUMBER()", executed_sql)
        self.assertIn("PARTITION BY fs.ticker", executed_sql)
        self.assertIn("JOIN job_runs", executed_sql)
        self.assertIn("WHERE rn = 1", executed_sql)
        self.assertEqual(executed_params, (25,))

    def test_default_limit_is_25(self):
        conn, cursor = self._fake_conn()
        queries.get_latest_factor_scores(conn)
        self.assertEqual(cursor.execute.call_args[0][1], (25,))


if __name__ == "__main__":
    unittest.main()
