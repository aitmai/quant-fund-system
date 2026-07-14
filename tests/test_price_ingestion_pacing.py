import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.ingestion import price_ingestion
from src.providers.price_provider_base import AllProvidersUnavailableError, PriceBar


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

    @patch("src.ingestion.price_ingestion.time.sleep")
    @patch("src.ingestion.price_ingestion.budget")
    @patch("src.ingestion.price_ingestion.fetch_with_fallback")
    @patch("src.ingestion.price_ingestion.mark_failure")
    @patch("src.ingestion.price_ingestion.get_candidates")
    @patch("src.ingestion.price_ingestion.seed_new_tickers")
    def test_stops_early_after_sustained_failure_streak(
        self, mock_seed, mock_get_candidates, mock_mark_failure,
        mock_fetch, mock_budget, mock_sleep,
    ):
        from src.providers.price_provider_base import PriceProviderError

        # 50 candidates, but every single one fails — should stop after
        # MAX_CONSECUTIVE_FAILURES rather than grinding through all 50.
        mock_budget.remaining_ticker_budget.return_value = (50, "")
        mock_get_candidates.return_value = [(f"TICK{i}", None, 0) for i in range(50)]
        mock_fetch.side_effect = PriceProviderError("rate limit", retryable=True)

        summary = price_ingestion.run(MagicMock())

        self.assertEqual(summary["failed"], price_ingestion.MAX_CONSECUTIVE_FAILURES)
        self.assertLess(summary["processed"], 50)
        self.assertIn("consecutive failures", summary["stopped_early_reason"])

    @patch("src.ingestion.price_ingestion.time.sleep")
    @patch("src.ingestion.price_ingestion.budget")
    @patch("src.ingestion.price_ingestion.fetch_with_fallback")
    @patch("src.ingestion.price_ingestion.mark_success")
    @patch("src.ingestion.price_ingestion.mark_failure")
    @patch("src.ingestion.price_ingestion.upsert_price_bars")
    @patch("src.ingestion.price_ingestion.get_candidates")
    @patch("src.ingestion.price_ingestion.seed_new_tickers")
    def test_a_success_resets_the_failure_streak(
        self, mock_seed, mock_get_candidates, mock_upsert, mock_mark_failure,
        mock_mark_success, mock_fetch, mock_budget, mock_sleep,
    ):
        from src.providers.price_provider_base import PriceProviderError, PriceBar

        # Interleave failures with one success partway through — the streak
        # counter should reset on that success, so it takes MAX_CONSECUTIVE_FAILURES
        # more failures AFTER it to trip the early stop, not just the total count.
        candidates = [(f"FAIL{i}", None, 0) for i in range(price_ingestion.MAX_CONSECUTIVE_FAILURES - 1)]
        candidates.append(("GOOD", None, 0))
        candidates += [(f"FAIL2_{i}", None, 0) for i in range(price_ingestion.MAX_CONSECUTIVE_FAILURES)]

        mock_get_candidates.return_value = candidates
        mock_budget.remaining_ticker_budget.return_value = (len(candidates), "")

        def fetch_side_effect(conn, ticker, start_date, end_date):
            if ticker == "GOOD":
                return [PriceBar(ticker, date(2026, 1, 2), 1, 1, 1, 1, 1, 100)], "yfinance"
            raise PriceProviderError("rate limit", retryable=True)

        mock_fetch.side_effect = fetch_side_effect

        summary = price_ingestion.run(MagicMock())

        self.assertEqual(summary["succeeded"], 1)
        # Should have processed the first streak + the success + a full
        # second streak before stopping — not stopped by the first streak alone.
        self.assertGreater(summary["processed"], price_ingestion.MAX_CONSECUTIVE_FAILURES)


    @patch("src.ingestion.price_ingestion.time.sleep")
    @patch("src.ingestion.price_ingestion.budget")
    @patch("src.ingestion.price_ingestion.fetch_with_fallback")
    @patch("src.ingestion.price_ingestion.mark_failure")
    @patch("src.ingestion.price_ingestion.get_candidates")
    @patch("src.ingestion.price_ingestion.seed_new_tickers")
    def test_all_providers_unavailable_stops_immediately_without_retry_penalty(
        self, mock_seed, mock_get_candidates, mock_mark_failure,
        mock_fetch, mock_budget, mock_sleep,
    ):
        # Confirmed live 2026-07-14: once every provider is circuit-broken,
        # every remaining candidate would fail identically — this should
        # stop on the FIRST such failure (not wait for MAX_CONSECUTIVE_FAILURES),
        # and the ticker that hit this shouldn't be charged a retry_count
        # or a budget call, since no actual HTTP request was made for it.
        mock_budget.remaining_ticker_budget.return_value = (50, "")
        mock_get_candidates.return_value = [(f"TICK{i}", None, 0) for i in range(50)]
        mock_fetch.side_effect = AllProvidersUnavailableError(
            "No provider available — all circuit-broken for the rest of this run."
        )

        summary = price_ingestion.run(MagicMock())

        mock_mark_failure.assert_not_called()
        mock_budget.record_usage.assert_not_called()
        self.assertEqual(summary["processed"], 0)
        self.assertIn("providers are unavailable", summary["stopped_early_reason"])


if __name__ == "__main__":
    unittest.main()
