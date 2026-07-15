import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.scoring import compute_labels


def _price_series(ticker, dates, prices):
    # Deliberately plain `date` objects, NOT pd.to_datetime() — this
    # matches what _fetch_price_history_for_labeling() actually produces
    # from real psycopg2 rows (a DataFrame built straight from tuples,
    # dtype=object date column), not a datetime64 column. Using
    # pd.to_datetime() here silently tested against a shape the
    # production code never actually sees.
    return pd.DataFrame({"ticker": [ticker] * len(dates), "date": dates, "price": prices}).reset_index(drop=True)


class TestForwardReturnForTicker(unittest.TestCase):
    def test_basic_forward_return_uses_trading_day_offset_not_calendar_days(self):
        # 10 consecutive trading-day rows, deliberately NOT tagged with
        # real calendar spacing (irrelevant here) — horizon=3 must count
        # 3 ROWS forward in this ticker's own series, not 3 calendar days.
        dates = [date(2026, 1, d) for d in range(1, 11)]
        prices = [100, 101, 102, 103, 104, 105, 106, 107, 108, 109]
        df = _price_series("AAA", dates, prices)

        result = compute_labels._forward_return_for_ticker(df, date(2026, 1, 1), horizon=3)
        # start=100 (row 0), end=row 0+3=row 3 -> 103
        expected = (103 - 100) / 100 * 100.0
        self.assertAlmostEqual(result, expected, places=6)

    def test_score_date_not_exact_match_uses_nearest_at_or_before(self):
        # score_date falls on a date with NO price row (e.g. a holiday) —
        # must use the nearest date AT OR BEFORE it, never after (that
        # would leak future information into the "start" observation).
        dates = [date(2026, 1, 1), date(2026, 1, 5), date(2026, 1, 6)]
        prices = [100.0, 110.0, 120.0]
        df = _price_series("AAA", dates, prices)

        # 2026-01-03 has no row; nearest at-or-before is 2026-01-01 (100.0).
        result = compute_labels._forward_return_for_ticker(df, date(2026, 1, 3), horizon=1)
        expected = (110.0 - 100.0) / 100.0 * 100.0
        self.assertAlmostEqual(result, expected, places=6)

    def test_forward_window_not_yet_concluded_returns_none(self):
        dates = [date(2026, 1, 1), date(2026, 1, 2)]
        prices = [100.0, 101.0]
        df = _price_series("AAA", dates, prices)

        # horizon=5 would need a row 5 positions past the start — doesn't exist yet.
        result = compute_labels._forward_return_for_ticker(df, date(2026, 1, 1), horizon=5)
        self.assertIsNone(result)

    def test_no_price_data_at_all_returns_none(self):
        result = compute_labels._forward_return_for_ticker(None, date(2026, 1, 1), horizon=5)
        self.assertIsNone(result)

    def test_no_price_on_or_before_score_date_returns_none(self):
        # All price data starts AFTER score_date — using it would leak
        # future information, so this must return None, not the earliest
        # available price.
        dates = [date(2026, 6, 1), date(2026, 6, 2)]
        prices = [100.0, 101.0]
        df = _price_series("AAA", dates, prices)

        result = compute_labels._forward_return_for_ticker(df, date(2026, 1, 1), horizon=1)
        self.assertIsNone(result)

    def test_zero_start_price_returns_none_not_divide_by_zero(self):
        dates = [date(2026, 1, 1), date(2026, 1, 2)]
        prices = [0.0, 101.0]
        df = _price_series("AAA", dates, prices)

        result = compute_labels._forward_return_for_ticker(df, date(2026, 1, 1), horizon=1)
        self.assertIsNone(result)


class TestComputeLabels(unittest.TestCase):
    def _fake_conn_with_pending(self, pending_rows):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = pending_rows
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    @patch("src.scoring.compute_labels._fetch_price_history_for_labeling")
    def test_no_pending_candidates_returns_zeroed_summary(self, mock_fetch_prices):
        conn, cursor = self._fake_conn_with_pending([])
        summary = compute_labels.compute_labels(conn, horizon=21)
        self.assertEqual(summary, {"candidates": 0, "labeled": 0, "not_yet_computable": 0})
        mock_fetch_prices.assert_not_called()

    @patch("src.scoring.compute_labels._upsert_training_labels")
    @patch("src.scoring.compute_labels._fetch_price_history_for_labeling")
    def test_top_decile_label_is_relative_to_that_score_dates_cohort_not_global(
        self, mock_fetch_prices, mock_upsert
    ):
        # 10 tickers, same score_date. Forward returns spread 1%..10% —
        # with TOP_DECILE_THRESHOLD=0.90, only the single best performer
        # (10%) should get outperform_label=1; the rest get 0.
        score_date = date(2026, 1, 1)
        pending_rows = [(f"T{i}", score_date) for i in range(10)]
        conn, cursor = self._fake_conn_with_pending(pending_rows)

        dates = [date(2026, 1, 1), date(2026, 1, 2)]
        price_rows = []
        for i in range(10):
            # start=100 for everyone; end price gives forward return = i+1 %
            price_rows.append(_price_series(f"T{i}", dates, [100.0, 100.0 + (i + 1)]))
        mock_fetch_prices.return_value = pd.concat(price_rows, ignore_index=True)

        summary = compute_labels.compute_labels(conn, horizon=1)

        self.assertEqual(summary["candidates"], 10)
        self.assertEqual(summary["labeled"], 10)
        self.assertEqual(summary["not_yet_computable"], 0)

        upserted_df = mock_upsert.call_args[0][1]
        top_performer = upserted_df[upserted_df["ticker"] == "T9"].iloc[0]  # 10% return, the best
        self.assertEqual(top_performer["outperform_label"], 1)
        worst_performer = upserted_df[upserted_df["ticker"] == "T0"].iloc[0]  # 1% return, the worst
        self.assertEqual(worst_performer["outperform_label"], 0)

    @patch("src.scoring.compute_labels._upsert_training_labels")
    @patch("src.scoring.compute_labels._fetch_price_history_for_labeling")
    def test_not_yet_computable_tickers_excluded_from_upsert_not_labeled_zero(
        self, mock_fetch_prices, mock_upsert
    ):
        # A ticker with no forward window concluded yet must be counted
        # as "not_yet_computable", NOT silently upserted with a fabricated
        # label of 0 — that would be indistinguishable from "genuinely
        # underperformed" in the training set.
        score_date = date(2026, 1, 1)
        pending_rows = [("HASDATA", score_date), ("TOOSOON", score_date)]
        conn, cursor = self._fake_conn_with_pending(pending_rows)

        has_data_dates = [date(2026, 1, 1), date(2026, 1, 2)]
        too_soon_dates = [date(2026, 1, 1)]  # no forward row at all
        mock_fetch_prices.return_value = pd.concat([
            _price_series("HASDATA", has_data_dates, [100.0, 105.0]),
            _price_series("TOOSOON", too_soon_dates, [100.0]),
        ], ignore_index=True)

        summary = compute_labels.compute_labels(conn, horizon=1)

        self.assertEqual(summary["candidates"], 2)
        self.assertEqual(summary["labeled"], 1)
        self.assertEqual(summary["not_yet_computable"], 1)

        upserted_df = mock_upsert.call_args[0][1]
        self.assertNotIn("TOOSOON", upserted_df["ticker"].values)


if __name__ == "__main__":
    unittest.main()
