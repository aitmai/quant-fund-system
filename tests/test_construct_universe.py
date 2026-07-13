import unittest
from unittest.mock import MagicMock, patch

from src.universe import construct_universe


def _fake_conn():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


class TestUniverseCikPersistence(unittest.TestCase):
    @patch("src.universe.construct_universe.index_sources")
    def test_new_ticker_insert_includes_cik(self, mock_index_sources):
        mock_index_sources.fetch_sp500_constituents.return_value = [
            {"ticker": "AAPL", "company_name": "Apple Inc.", "sector": "Tech", "cik": "0000320193"}
        ]
        conn, cursor = _fake_conn()
        cursor.fetchall.return_value = []  # no existing auto tickers

        construct_universe.sync_index_universe(conn, include_sp500=True, include_russell1000=False)

        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO universe (" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        # params tuple: (ticker, company_name, sector, today, cik)
        params = insert_calls[0][0][1]
        self.assertIn("0000320193", params)

    @patch("src.universe.construct_universe.index_sources")
    def test_backfills_cik_for_already_existing_ticker(self, mock_index_sources):
        # AAPL already exists (was synced before CIK column existed) —
        # sync should backfill cik via UPDATE, not just on INSERT.
        mock_index_sources.fetch_sp500_constituents.return_value = [
            {"ticker": "AAPL", "company_name": "Apple Inc.", "sector": "Tech", "cik": "0000320193"}
        ]
        conn, cursor = _fake_conn()
        cursor.fetchall.return_value = [("AAPL",)]  # already an active auto ticker

        construct_universe.sync_index_universe(conn, include_sp500=True, include_russell1000=False)

        backfill_calls = [
            c for c in cursor.execute.call_args_list
            if "UPDATE universe SET cik" in c[0][0]
        ]
        self.assertEqual(len(backfill_calls), 1)
        self.assertEqual(backfill_calls[0][0][1], ("0000320193", "AAPL"))

    @patch("src.universe.construct_universe.index_sources")
    def test_no_backfill_attempted_when_source_has_no_cik(self, mock_index_sources):
        # Russell-1000-only (iShares) rows have no cik key at all.
        mock_index_sources.fetch_sp500_constituents.return_value = []
        mock_index_sources.fetch_russell1000_constituents.return_value = [
            {"ticker": "AAPL", "company_name": "Apple Inc.", "sector": "Tech"}
        ]
        conn, cursor = _fake_conn()
        cursor.fetchall.return_value = [("AAPL",)]

        construct_universe.sync_index_universe(conn, include_sp500=False, include_russell1000=True)

        backfill_calls = [c for c in cursor.execute.call_args_list if "UPDATE universe SET cik" in c[0][0]]
        self.assertEqual(len(backfill_calls), 0)


if __name__ == "__main__":
    unittest.main()
