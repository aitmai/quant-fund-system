import importlib.util
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location(
    "backfill_factor_scores", "scripts/backfill_factor_scores.py"
)
backfill_factor_scores = importlib.util.module_from_spec(spec)
sys.modules["backfill_factor_scores"] = backfill_factor_scores
spec.loader.exec_module(backfill_factor_scores)


class TestWeeklyMondays(unittest.TestCase):
    def test_start_already_monday_included(self):
        # 2026-01-05 is a Monday.
        dates = backfill_factor_scores.weekly_mondays(date(2026, 1, 5), date(2026, 1, 5))
        self.assertEqual(dates, [date(2026, 1, 5)])

    def test_start_not_monday_advances_to_first_monday(self):
        # 2026-01-01 is a Thursday; first Monday on/after is 2026-01-05.
        dates = backfill_factor_scores.weekly_mondays(date(2026, 1, 1), date(2026, 1, 5))
        self.assertEqual(dates, [date(2026, 1, 5)])

    def test_weekly_spacing_across_range(self):
        dates = backfill_factor_scores.weekly_mondays(date(2026, 1, 5), date(2026, 1, 26))
        self.assertEqual(dates, [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19), date(2026, 1, 26)])

    def test_end_before_first_monday_returns_empty(self):
        dates = backfill_factor_scores.weekly_mondays(date(2026, 1, 1), date(2026, 1, 2))
        self.assertEqual(dates, [])


class TestAlreadyBackfilledDates(unittest.TestCase):
    def test_query_filters_by_backfill_triggered_by_and_complete_status(self):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2026, 1, 5),), (date(2026, 1, 12),)]
        conn.cursor.return_value.__enter__.return_value = cursor

        result = backfill_factor_scores.already_backfilled_dates(conn)

        executed_sql = cursor.execute.call_args[0][0]
        executed_params = cursor.execute.call_args[0][1]
        self.assertIn("jr.triggered_by = %s", executed_sql)
        self.assertIn("jr.status = 'complete'", executed_sql)
        self.assertEqual(executed_params, (backfill_factor_scores.BACKFILL_TRIGGERED_BY,))
        self.assertEqual(result, {date(2026, 1, 5), date(2026, 1, 12)})


class TestRunBackfill(unittest.TestCase):
    @patch("backfill_factor_scores.JobRun")
    @patch("backfill_factor_scores.run_factor_scoring")
    def test_one_bad_date_does_not_abort_the_whole_backfill(self, mock_rfs, mock_jobrun_cls):
        # 3 dates; the middle one raises. Both good dates should still be
        # processed — one failure must not stall the rest, same
        # philosophy as the ingestion crons' "don't let one ticker's
        # failure kill the whole run" behavior.
        mock_job = MagicMock()
        mock_jobrun_cls.return_value.__enter__.return_value = mock_job
        mock_jobrun_cls.return_value.__exit__.return_value = False

        def run_side_effect(conn, job=None, score_date=None):
            if score_date == date(2026, 1, 12):
                raise RuntimeError("simulated failure")
            return {"scored_at_least_one": 100, "total_active": 100}

        mock_rfs.run.side_effect = run_side_effect

        conn = MagicMock()
        dates = [date(2026, 1, 5), date(2026, 1, 12), date(2026, 1, 19)]
        summary = backfill_factor_scores.run_backfill(conn, dates)

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(mock_rfs.run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
