import unittest
from datetime import date
from unittest.mock import MagicMock

from src.ingestion import budget


def _fake_conn(config_row, usage_row=None, rolling_sum=0):
    conn = MagicMock()
    cursor = MagicMock()

    def execute(sql, params=None):
        cursor._last_sql = sql

    def fetchone():
        sql = cursor._last_sql.lower()
        if "from ingestion_config" in sql:
            return config_row
        if "from ingestion_daily_usage where data_type" in sql and "sum" not in sql:
            return usage_row
        if "sum(bandwidth_used_mb)" in sql:
            return (rolling_sum,)
        return None

    cursor.execute.side_effect = execute
    cursor.fetchone.side_effect = fetchone
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


class TestBudget(unittest.TestCase):
    def test_full_budget_available_on_fresh_day(self):
        # daily_budget=400, calls_per_ticker=1, no bandwidth cap, no usage yet
        conn = _fake_conn(config_row=(400, 1, None, "yfinance"), usage_row=None)
        remaining, reason = budget.remaining_ticker_budget(conn, "price", today=date(2026, 1, 1))
        self.assertEqual(remaining, 400)
        self.assertEqual(reason, "")

    def test_budget_reduced_by_calls_per_ticker(self):
        # fundamentals: daily_budget=225, calls_per_ticker=3 -> 75 tickers/day
        conn = _fake_conn(config_row=(225, 3, 500, "yfinance"), usage_row=None, rolling_sum=0)
        remaining, reason = budget.remaining_ticker_budget(conn, "fundamentals", today=date(2026, 1, 1))
        self.assertEqual(remaining, 75)

    def test_budget_exhausted_returns_zero_with_reason(self):
        conn = _fake_conn(config_row=(400, 1, None, "yfinance"), usage_row=(400,))
        remaining, reason = budget.remaining_ticker_budget(conn, "price", today=date(2026, 1, 1))
        self.assertEqual(remaining, 0)
        self.assertIn("daily request budget exhausted", reason)

    def test_bandwidth_cap_blocks_even_with_request_budget_left(self):
        # daily budget not exhausted, but rolling 30-day bandwidth cap is
        conn = _fake_conn(config_row=(225, 3, 500, "yfinance"), usage_row=(0,), rolling_sum=500)
        remaining, reason = budget.remaining_ticker_budget(conn, "fundamentals", today=date(2026, 1, 1))
        self.assertEqual(remaining, 0)
        self.assertIn("bandwidth cap", reason)

    def test_missing_config_row_raises(self):
        conn = _fake_conn(config_row=None)
        with self.assertRaises(RuntimeError):
            budget.remaining_ticker_budget(conn, "price", today=date(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
