import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.scoring import run_factor_scoring


def _fake_universe(tickers_sectors):
    return pd.DataFrame(tickers_sectors, columns=["ticker", "sector"])


class TestRunFactorScoring(unittest.TestCase):
    @patch("src.scoring.run_factor_scoring.data_fetch")
    def test_no_active_tickers_skips_cleanly(self, mock_data_fetch):
        mock_data_fetch.fetch_active_universe.return_value = pd.DataFrame(columns=["ticker", "sector"])
        conn = MagicMock()

        summary = run_factor_scoring.run(conn)

        self.assertEqual(summary["total_active"], 0)
        self.assertEqual(summary["scored"], 0)

    @patch("src.scoring.run_factor_scoring.data_fetch")
    def test_diagnostic_summary_counts_missing_against_total_active(self, mock_data_fetch):
        # 4 active tickers total, 2 sectors. AAA and DDD (Tech) both have
        # enough price history for momentum/lowvol; BBB/CCC don't. Two
        # Tech tickers is required for sector_zscore to actually produce
        # a value (n>=2 per sector) — a single qualifying ticker in its
        # sector would itself be excluded, a real and separate edge case
        # covered by test_lone_qualifying_ticker_in_sector_still_excluded.
        mock_data_fetch.fetch_active_universe.return_value = _fake_universe(
            [("AAA", "Tech"), ("BBB", "Tech"), ("CCC", "Tech"), ("DDD", "Tech")]
        )

        def _sufficient_prices(offset):
            prices = [1.0] * 300
            prices[-253] = 100.0 + offset
            prices[-22] = 150.0 + offset
            return prices

        dates = pd.date_range("2024-01-01", periods=300)
        price_panel = pd.concat([
            pd.DataFrame({"ticker": ["AAA"] * 300, "date": dates, "close": _sufficient_prices(0),
                          "adj_close": _sufficient_prices(0), "price": _sufficient_prices(0)}),
            pd.DataFrame({"ticker": ["DDD"] * 300, "date": dates, "close": _sufficient_prices(10),
                          "adj_close": _sufficient_prices(10), "price": _sufficient_prices(10)}),
        ])
        mock_data_fetch.fetch_price_panel.return_value = price_panel

        fundamentals_panel = pd.DataFrame([{
            "ticker": "BBB", "report_date": date(2025, 12, 31), "filed_date": date(2026, 2, 1),
            "roe": 0.2, "ev_ebitda": 10.0, "fcf_yield": 0.05, "debt_equity": 0.5, "earnings_variance": 0.1,
        }])
        mock_data_fetch.fetch_latest_fundamentals.return_value = fundamentals_panel

        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        summary = run_factor_scoring.run(conn, score_date=date(2026, 7, 13))

        self.assertEqual(summary["total_active"], 4)
        # AAA and DDD scored for momentum/lowvol; BBB/CCC did not -> 2/4 missing.
        self.assertEqual(summary["momentum"]["missing"], 2)
        self.assertEqual(summary["lowvol"]["missing"], 2)
        # Only BBB has fundamentals, and it's alone in its sector for
        # quality/value purposes -> excluded by sector_zscore too (n<2),
        # so quality/value end up 0/4 populated here. The real assertion
        # is that the denominator is total_active (4), not a smaller subset.
        self.assertEqual(summary["quality"]["missing"] + summary["quality"]["populated"], 4)

    @patch("src.scoring.run_factor_scoring.data_fetch")
    def test_lone_qualifying_ticker_in_sector_still_excluded(self, mock_data_fetch):
        # AAA has PLENTY of raw price history, but is the only Tech ticker
        # with any — sector_zscore correctly excludes it (n<2), even
        # though its raw momentum/lowvol values exist. This is a real,
        # intentional consequence of sector-relative scoring, not a bug —
        # locking it in explicitly so it isn't "fixed" by accident later.
        mock_data_fetch.fetch_active_universe.return_value = _fake_universe([("AAA", "Tech")])
        prices = [1.0] * 300
        prices[-253] = 100.0
        prices[-22] = 150.0
        dates = pd.date_range("2024-01-01", periods=300)
        mock_data_fetch.fetch_price_panel.return_value = pd.DataFrame({
            "ticker": ["AAA"] * 300, "date": dates, "close": prices, "adj_close": prices, "price": prices,
        })
        mock_data_fetch.fetch_latest_fundamentals.return_value = pd.DataFrame(
            columns=["ticker", "report_date", "filed_date", "roe", "ev_ebitda", "fcf_yield", "debt_equity", "earnings_variance"]
        )
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        summary = run_factor_scoring.run(conn, score_date=date(2026, 7, 13))

        self.assertEqual(summary["momentum"]["populated"], 0)
        self.assertEqual(summary["momentum"]["missing"], 1)

    @patch("src.scoring.run_factor_scoring.data_fetch")
    def test_upsert_writes_one_row_per_active_ticker(self, mock_data_fetch):
        mock_data_fetch.fetch_active_universe.return_value = _fake_universe([("AAA", "Tech"), ("BBB", "Tech")])
        mock_data_fetch.fetch_price_panel.return_value = pd.DataFrame(columns=["ticker", "date", "close", "adj_close", "price"])
        mock_data_fetch.fetch_latest_fundamentals.return_value = pd.DataFrame(
            columns=["ticker", "report_date", "filed_date", "roe", "ev_ebitda", "fcf_yield", "debt_equity", "earnings_variance"]
        )

        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor

        run_factor_scoring.run(conn, score_date=date(2026, 7, 13))

        executemany_calls = cursor.executemany.call_args_list
        self.assertEqual(len(executemany_calls), 1)
        rows_written = executemany_calls[0][0][1]
        self.assertEqual(len(rows_written), 2)  # one row per active ticker, even with no scores

    @patch("src.scoring.run_factor_scoring.data_fetch")
    def test_job_note_called_with_summary_when_job_provided(self, mock_data_fetch):
        mock_data_fetch.fetch_active_universe.return_value = _fake_universe([("AAA", "Tech")])
        mock_data_fetch.fetch_price_panel.return_value = pd.DataFrame(columns=["ticker", "date", "close", "adj_close", "price"])
        mock_data_fetch.fetch_latest_fundamentals.return_value = pd.DataFrame(
            columns=["ticker", "report_date", "filed_date", "roe", "ev_ebitda", "fcf_yield", "debt_equity", "earnings_variance"]
        )
        conn = MagicMock()
        cursor = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        job = MagicMock()
        job.run_type = "manual"
        job.run_id = "test-run-id"

        run_factor_scoring.run(conn, job=job, score_date=date(2026, 7, 13))

        job.note.assert_called_once()


if __name__ == "__main__":
    unittest.main()
