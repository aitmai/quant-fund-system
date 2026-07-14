import unittest
from unittest.mock import MagicMock, patch

from src.ingestion import fundamentals_ingestion


class TestFundamentalsIngestionEarlyStop(unittest.TestCase):
    @patch("src.ingestion.fundamentals_ingestion.time.sleep")
    @patch("src.ingestion.fundamentals_ingestion.budget")
    @patch("src.ingestion.fundamentals_ingestion.SECEdgarProvider")
    @patch("src.ingestion.fundamentals_ingestion.get_candidates")
    @patch("src.ingestion.fundamentals_ingestion.seed_new_tickers")
    def test_stops_early_after_sustained_failure_streak(
        self, mock_seed, mock_get_candidates, mock_provider_cls, mock_budget, mock_sleep
    ):
        from src.providers.price_provider_base import PriceProviderError

        mock_budget.remaining_ticker_budget.return_value = (50, "")
        mock_budget.get_config.return_value = {"calls_per_ticker": 1}
        mock_get_candidates.return_value = [(f"TICK{i}", 0, f"000000{i:04d}") for i in range(50)]

        mock_provider_instance = MagicMock()
        mock_provider_instance.fetch_fundamentals.side_effect = PriceProviderError(
            "EDGAR outage", retryable=True
        )
        mock_provider_cls.return_value = mock_provider_instance

        summary = fundamentals_ingestion.run(MagicMock())

        self.assertEqual(summary["failed"], fundamentals_ingestion.MAX_CONSECUTIVE_FAILURES)
        self.assertLess(summary["processed"], 50)
        self.assertIn("consecutive failures", summary["stopped_early_reason"])

    @patch("src.ingestion.fundamentals_ingestion.time.sleep")
    @patch("src.ingestion.fundamentals_ingestion.budget")
    @patch("src.ingestion.fundamentals_ingestion.SECEdgarProvider")
    @patch("src.ingestion.fundamentals_ingestion.upsert_fundamentals_rows")
    @patch("src.ingestion.fundamentals_ingestion.get_candidates")
    @patch("src.ingestion.fundamentals_ingestion.seed_new_tickers")
    def test_success_resets_the_failure_streak(
        self, mock_seed, mock_get_candidates, mock_upsert, mock_provider_cls, mock_budget, mock_sleep
    ):
        from src.providers.price_provider_base import PriceProviderError

        n = fundamentals_ingestion.MAX_CONSECUTIVE_FAILURES
        candidates = [(f"FAIL{i}", 0, f"000000{i:04d}") for i in range(n - 1)]
        candidates.append(("GOOD", 0, "0000000000"))
        candidates += [(f"FAIL2_{i}", 0, f"000000{i:04d}") for i in range(n)]

        mock_get_candidates.return_value = candidates
        mock_budget.remaining_ticker_budget.return_value = (len(candidates), "")
        mock_budget.get_config.return_value = {"calls_per_ticker": 1}

        mock_provider_instance = MagicMock()

        def fetch_side_effect(ticker, cik, conn=None):
            if ticker == "GOOD":
                return []
            raise PriceProviderError("EDGAR outage", retryable=True)

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

    @patch("src.ingestion.fundamentals_ingestion.time.sleep")
    @patch("src.ingestion.fundamentals_ingestion.budget")
    @patch("src.ingestion.fundamentals_ingestion.SECEdgarProvider")
    @patch("src.ingestion.fundamentals_ingestion.mark_failure")
    @patch("src.ingestion.fundamentals_ingestion.get_candidates")
    @patch("src.ingestion.fundamentals_ingestion.seed_new_tickers")
    def test_missing_cik_skipped_without_api_call_or_streak_impact(
        self, mock_seed, mock_get_candidates, mock_mark_failure, mock_provider_cls, mock_budget, mock_sleep
    ):
        # A ticker with no CIK (e.g. a manually-added one with no CIK
        # supplied) should be skipped cleanly — no HTTP call attempted,
        # and it should NOT count toward the consecutive-failure streak
        # (missing data for one ticker isn't a signal of provider health).
        mock_budget.remaining_ticker_budget.return_value = (10, "")
        mock_budget.get_config.return_value = {"calls_per_ticker": 1}
        mock_get_candidates.return_value = [("NOCIK", 0, None), ("NOCIK2", 0, "")]

        mock_provider_instance = MagicMock()
        mock_provider_cls.return_value = mock_provider_instance

        summary = fundamentals_ingestion.run(MagicMock())

        mock_provider_instance.fetch_fundamentals.assert_not_called()
        self.assertEqual(summary["skipped_no_cik"], 2)
        self.assertEqual(summary["stopped_early_reason"], "")


class TestGetCandidatesQuery(unittest.TestCase):
    """Same fix, same rationale as price_ingestion.py's equivalent test:
    a ticker whose retry_count reaches MAX_RETRY_COUNT was previously
    excluded from every future candidate query, forever. Verifies the
    real SQL (not a mock of get_candidates) includes the cooldown clause."""

    def _fake_conn(self, rows=None):
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = rows or []
        conn.cursor.return_value.__enter__.return_value = cursor
        return conn, cursor

    def test_query_includes_failed_retry_cooldown_clause(self):
        conn, cursor = self._fake_conn()
        fundamentals_ingestion.get_candidates(conn, 100)

        executed_sql = cursor.execute.call_args[0][0]
        executed_params = cursor.execute.call_args[0][1]

        self.assertIn("fetch_status = 'failed'", executed_sql)
        self.assertIn("last_attempt_at < NOW() - INTERVAL", executed_sql)
        self.assertIn(fundamentals_ingestion.FAILED_RETRY_COOLDOWN_DAYS, executed_params)


if __name__ == "__main__":
    unittest.main()
