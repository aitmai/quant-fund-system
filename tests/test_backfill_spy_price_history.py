import importlib.util
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers.price_provider_base import PriceBar

sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location(
    "backfill_spy_price_history", "scripts/backfill_spy_price_history.py"
)
backfill_spy_price_history = importlib.util.module_from_spec(spec)
sys.modules["backfill_spy_price_history"] = backfill_spy_price_history
spec.loader.exec_module(backfill_spy_price_history)

ensure_spy_in_universe = backfill_spy_price_history.ensure_spy_in_universe
backfill_spy_prices = backfill_spy_price_history.backfill_spy_prices


class TestEnsureSpyInUniverse(unittest.TestCase):
    def test_inserts_spy_with_is_active_false(self):
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        ensure_spy_in_universe(mock_conn)

        sql, params = mock_cursor.execute.call_args[0]
        self.assertIn("FALSE", sql)  # is_active = FALSE, not TRUE
        self.assertIn("ON CONFLICT (ticker) DO NOTHING", sql)
        self.assertEqual(params[0], "SPY")

    def test_never_flips_is_active_true_on_conflict(self):
        # Regression guard: unlike upload_manual_tickers(), this must
        # use DO NOTHING on conflict, never DO UPDATE SET is_active = TRUE
        # — SPY existing already (e.g. added some other way) must not be
        # silently swept into the active-universe cron loops.
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        ensure_spy_in_universe(mock_conn)

        sql, _ = mock_cursor.execute.call_args[0]
        self.assertNotIn("DO UPDATE", sql)


class TestBackfillSpyPrices(unittest.TestCase):
    @patch("backfill_spy_price_history.YFinanceProvider")
    def test_fetches_and_upserts_bars(self, mock_provider_cls):
        mock_provider = MagicMock()
        mock_provider.fetch_history.return_value = [
            PriceBar("SPY", date(2026, 7, 10), 100, 101, 99, 100.5, 1000000, 100.5),
            PriceBar("SPY", date(2026, 7, 13), 100.5, 102, 100, 101.5, 1200000, 101.5),
        ]
        mock_provider_cls.return_value = mock_provider

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        count = backfill_spy_prices(mock_conn, years=3.0)

        self.assertEqual(count, 2)
        mock_provider.fetch_history.assert_called_once()
        self.assertEqual(mock_provider.fetch_history.call_args[0][0], "SPY")
        mock_cursor.executemany.assert_called_once()

    @patch("backfill_spy_price_history.YFinanceProvider")
    def test_no_bars_returns_zero_without_db_call(self, mock_provider_cls):
        mock_provider = MagicMock()
        mock_provider.fetch_history.return_value = []
        mock_provider_cls.return_value = mock_provider

        mock_conn = MagicMock()
        count = backfill_spy_prices(mock_conn, years=3.0)

        self.assertEqual(count, 0)
        mock_conn.cursor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
