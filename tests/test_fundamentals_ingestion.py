import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.ingestion import fundamentals_ingestion


class TestFundamentalsIngestionEarlyStop(unittest.TestCase):
    @patch("src.ingestion.fundamentals_ingestion.budget")
    @patch("src.ingestion.fundamentals_ingestion.FMPFundamentalsProvider")
    @patch("src.ingestion.fundamentals_ingestion.get_candidates")
    @patch("src.ingestion.fundamentals_ingestion.seed_new_tickers")
    def test_stops_early_after_sustained_failure_streak(
        self, mock_seed, mock_get_candidates, mock_provider_cls, mock_budget
    ):
        from src.providers.price_provider_base import PriceProviderError

        mock_budget.remaining_ticker_budget.return_value = (50, "")
        mock_budget.get_config.return_value = {"calls_per_ticker": 3}
        mock_get_candidates.return_value = [(f"TICK{i}", 0) for i in range(50)]

        mock_provider_instance = MagicMock()
        mock_provider_instance.fetch_fundamentals.side_effect = PriceProviderError(
            "all endpoints plan-gated", retryable=False
        )
        mock_provider_cls.return_value = mock_provider_instance

        summary = fundamentals_ingestion.run(MagicMock())

        self.assertEqual(summary["failed"], fundamentals_ingestion.MAX_CONSECUTIVE_FAILURES)
        self.assertLess(summary["processed"], 50)
        self.assertIn("consecutive failures", summary["stopped_early_reason"])

    @patch("src.ingestion.fundamentals_ingestion.budget")
    @patch("src.ingestion.fundamentals_ingestion.FMPFundamentalsProvider")
    @patch("src.ingestion.fundamentals_ingestion.upsert_fundamentals_rows")
    @patch("src.ingestion.fundamentals_ingestion.get_candidates")
    @patch("src.ingestion.fundamentals_ingestion.seed_new_tickers")
    def test_success_resets_the_failure_streak(
        self, mock_seed, mock_get_candidates, mock_upsert, mock_provider_cls, mock_budget
    ):
        from src.providers.price_provider_base import PriceProviderError

        n = fundamentals_ingestion.MAX_CONSECUTIVE_FAILURES
        candidates = [(f"FAIL{i}", 0) for i in range(n - 1)]
        candidates.append(("GOOD", 0))
        candidates += [(f"FAIL2_{i}", 0) for i in range(n)]

        mock_get_candidates.return_value = candidates
        mock_budget.remaining_ticker_budget.return_value = (len(candidates), "")
        mock_budget.get_config.return_value = {"calls_per_ticker": 3}

        mock_provider_instance = MagicMock()

        def fetch_side_effect(ticker):
            if ticker == "GOOD":
                return []
            raise PriceProviderError("plan-gated", retryable=False)

        mock_provider_instance.fetch_fundamentals.side_effect = fetch_side_effect
        mock_provider_cls.return_value = mock_provider_instance

        summary = fundamentals_ingestion.run(MagicMock())

        self.assertEqual(summary["succeeded"], 1)
        self.assertGreater(summary["processed"], n)

    @patch("src.ingestion.fundamentals_ingestion.budget")
    @patch("src.ingestion.fundamentals_ingestion.get_candidates")
    @patch("src.ingestion.fundamentals_ingestion.seed_new_tickers")
    def test_no_early_stop_when_budget_exhausted_up_front(self, mock_seed, mock_get_candidates, mock_budget):
        mock_budget.remaining_ticker_budget.return_value = (0, "daily request budget exhausted")

        summary = fundamentals_ingestion.run(MagicMock())

        self.assertEqual(summary["stopped_early_reason"], "")
        mock_get_candidates.assert_not_called()


if __name__ == "__main__":
    unittest.main()
