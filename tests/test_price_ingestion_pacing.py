import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.ingestion import price_ingestion
from src.providers.price_provider_base import PriceBar


class TestPriceIngestionPacing(unittest.TestCase):
    @patch("src.ingestion.price_ingestion.time.sleep")
    @patch("src.ingestion.price_ingestion.budget")
    @patch("src.ingestion.price_ingestion.fetch_with_fallback")
    @patch("src.ingestion.price_ingestion.upsert_price_bars")
    @patch("src.ingestion.price_ingestion.mark_success")
    @patch("src.ingestion.price_ingestion.get_candidates")
    @patch("src.ingestion.price_ingestion.seed_new_tickers")
    def test_sleeps_between_every_ticker_to_avoid_throttling(
        self, mock_seed, mock_get_candidates, mock_mark_success,
        mock_upsert, mock_fetch, mock_budget, mock_sleep,
    ):
        mock_budget.remaining_ticker_budget.return_value = (10, "")
        mock_get_candidates.return_value = [
            ("AAPL", None, 0),
            ("MSFT", None, 0),
            ("TSCO", None, 0),
        ]
        mock_fetch.return_value = ([PriceBar("X", date(2026, 1, 2), 1, 1, 1, 1, 1, 100)], "yfinance")

        price_ingestion.run(MagicMock())

        # One sleep per ticker processed — this is the actual throttling fix.
        self.assertEqual(mock_sleep.call_count, 3)
        mock_sleep.assert_called_with(price_ingestion.REQUEST_DELAY_SECONDS)

    @patch("src.ingestion.price_ingestion.time.sleep")
    @patch("src.ingestion.price_ingestion.budget")
    @patch("src.ingestion.price_ingestion.get_candidates")
    @patch("src.ingestion.price_ingestion.seed_new_tickers")
    def test_no_sleep_when_budget_exhausted_before_any_fetch(
        self, mock_seed, mock_get_candidates, mock_budget, mock_sleep
    ):
        mock_budget.remaining_ticker_budget.return_value = (0, "daily request budget exhausted")

        price_ingestion.run(MagicMock())

        mock_sleep.assert_not_called()
        mock_get_candidates.assert_not_called()

    @patch("src.ingestion.price_ingestion.time.sleep")
    @patch("src.ingestion.price_ingestion.budget")
    @patch("src.ingestion.price_ingestion.fetch_with_fallback")
    @patch("src.ingestion.price_ingestion.mark_failure")
    @patch("src.ingestion.price_ingestion.get_candidates")
    @patch("src.ingestion.price_ingestion.seed_new_tickers")
    def test_sleeps_even_after_a_failed_ticker(
        self, mock_seed, mock_get_candidates, mock_mark_failure,
        mock_fetch, mock_budget, mock_sleep,
    ):
        from src.providers.price_provider_base import PriceProviderError

        mock_budget.remaining_ticker_budget.return_value = (10, "")
        mock_get_candidates.return_value = [("BLDR", None, 0)]
        mock_fetch.side_effect = PriceProviderError("throttled", retryable=True)

        price_ingestion.run(MagicMock())

        # Pacing has to apply on failures too, or a run dominated by
        # throttled failures wouldn't get any slower — defeating the point.
        mock_sleep.assert_called_once_with(price_ingestion.REQUEST_DELAY_SECONDS)


if __name__ == "__main__":
    unittest.main()
