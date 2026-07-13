import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers.fmp_fundamentals_provider import FMPFundamentalsProvider


class TestFMPFundamentalsMerge(unittest.TestCase):
    def setUp(self):
        self.provider = FMPFundamentalsProvider(api_key="fake-key")

    @patch.object(FMPFundamentalsProvider, "_get")
    def test_merges_three_endpoints_by_report_date(self, mock_get):
        def fake_get(path, ticker):
            if path == "ratios":
                return [{"date": "2025-12-31", "fillingDate": "2026-02-10", "returnOnEquity": 0.25, "debtEquityRatio": 1.1}]
            if path == "key-metrics":
                return [{"date": "2025-12-31", "enterpriseValueOverEBITDA": 12.5, "freeCashFlowYield": 0.04}]
            if path == "income-statement":
                return [
                    {"date": "2025-03-31", "fillingDate": "2025-05-01", "eps": 1.0},
                    {"date": "2025-06-30", "fillingDate": "2025-08-01", "eps": 1.2},
                    {"date": "2025-09-30", "fillingDate": "2025-11-01", "eps": 0.9},
                    {"date": "2025-12-31", "fillingDate": "2026-02-10", "eps": 1.3},
                ]
            return []

        mock_get.side_effect = fake_get
        rows = self.provider.fetch_fundamentals("TEST")

        row_by_date = {r.report_date: r for r in rows}
        q4 = row_by_date[date(2025, 12, 31)]
        self.assertEqual(q4.roe, 0.25)
        self.assertEqual(q4.debt_equity, 1.1)
        self.assertEqual(q4.ev_ebitda, 12.5)
        self.assertEqual(q4.fcf_yield, 0.04)
        self.assertEqual(q4.filed_date, date(2026, 2, 10))
        self.assertIsNotNone(q4.earnings_variance)  # 4 quarters of EPS available

        q1 = row_by_date[date(2025, 3, 31)]
        self.assertIsNone(q1.earnings_variance)  # only 1 quarter available, needs >=2

    @patch.object(FMPFundamentalsProvider, "_get")
    def test_handles_missing_endpoint_data_gracefully(self, mock_get):
        # ratios endpoint returns data, others are empty (e.g. plan limitation)
        def fake_get(path, ticker):
            if path == "ratios":
                return [{"date": "2025-12-31", "fillingDate": "2026-02-10", "returnOnEquity": 0.1, "debtEquityRatio": 0.5}]
            return []

        mock_get.side_effect = fake_get
        rows = self.provider.fetch_fundamentals("TEST")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].roe, 0.1)
        self.assertIsNone(rows[0].ev_ebitda)
        self.assertIsNone(rows[0].fcf_yield)

    @patch.object(FMPFundamentalsProvider, "_get")
    def test_new_stable_field_names_are_picked_up(self, mock_get):
        # Simulates FMP's 2026 /stable/ field renames: 'roe' instead of
        # 'returnOnEquity', 'debtToEquity' instead of 'debtEquityRatio'.
        def fake_get(path, ticker):
            if path == "ratios":
                return [{"date": "2025-12-31", "fillingDate": "2026-02-10", "debtToEquity": 0.8}]
            if path == "key-metrics":
                return [{"date": "2025-12-31", "roe": 0.22, "evToEBITDA": 11.0, "fcfYield": 0.05}]
            return []

        mock_get.side_effect = fake_get
        rows = self.provider.fetch_fundamentals("TEST")
        row = rows[0]
        self.assertEqual(row.roe, 0.22)
        self.assertEqual(row.debt_equity, 0.8)
        self.assertEqual(row.ev_ebitda, 11.0)
        self.assertEqual(row.fcf_yield, 0.05)

    @patch.object(FMPFundamentalsProvider, "_get")
    def test_roe_found_via_key_metrics_when_absent_from_ratios(self, mock_get):
        # Reflects the reshuffle where ROE reportedly moved out of `ratios`.
        def fake_get(path, ticker):
            if path == "ratios":
                return [{"date": "2025-12-31", "debtEquityRatio": 1.0}]  # no roe here
            if path == "key-metrics":
                return [{"date": "2025-12-31", "returnOnEquity": 0.3}]  # old-style name, other endpoint
            return []

        mock_get.side_effect = fake_get
        rows = self.provider.fetch_fundamentals("TEST")
        self.assertEqual(rows[0].roe, 0.3)

    @patch("src.providers.fmp_fundamentals_provider.print")
    @patch.object(FMPFundamentalsProvider, "_get")
    def test_warns_when_a_field_never_matches_any_candidate_name(self, mock_get, mock_print):
        # None of the expected candidate keys for ev_ebitda are present —
        # should warn loudly rather than silently write NULLs.
        def fake_get(path, ticker):
            if path == "ratios":
                return [{"date": "2025-12-31", "roe": 0.1}]
            if path == "key-metrics":
                return [{"date": "2025-12-31", "someUnexpectedFieldName": 99}]
            return []

        mock_get.side_effect = fake_get
        self.provider.fetch_fundamentals("TEST")

        warning_calls = [c for c in mock_print.call_args_list if "ev_ebitda" in str(c) and "WARNING" in str(c)]
        self.assertTrue(warning_calls, "expected a WARNING print mentioning ev_ebitda")

    @patch.object(FMPFundamentalsProvider, "_get")
    def test_raises_when_all_three_endpoints_return_nothing(self, mock_get):
        # Changed from "returns []" — silently succeeding with zero data
        # would mark the ticker 'complete' forever, never retried. Raising
        # lets the normal retry_count/fetch_status mechanism try again later.
        mock_get.side_effect = lambda path, ticker: []
        from src.providers.price_provider_base import PriceProviderError

        with self.assertRaises(PriceProviderError):
            self.provider.fetch_fundamentals("TEST")

    @patch.object(FMPFundamentalsProvider, "_get")
    def test_one_failed_endpoint_does_not_abort_the_whole_ticker(self, mock_get):
        # ratios raises (e.g. plan-gated 402), but key-metrics and
        # income-statement both succeed — should still return usable rows
        # instead of losing the ticker entirely to one bad endpoint.
        from src.providers.price_provider_base import PriceProviderError

        def fake_get(path, ticker):
            if path == "ratios":
                raise PriceProviderError("FMP returned 402 for TEST (ratios): Premium...", retryable=False)
            if path == "key-metrics":
                return [{"date": "2025-12-31", "roe": 0.2, "evToEBITDA": 10.0, "fcfYield": 0.03}]
            return []

        mock_get.side_effect = fake_get
        rows = self.provider.fetch_fundamentals("TEST")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].roe, 0.2)
        self.assertEqual(rows[0].ev_ebitda, 10.0)

    def test_circuit_breaker_skips_confirmed_unavailable_endpoint(self):
        from src.providers import fmp_fundamentals_provider as module

        self.addCleanup(module.reset_circuit_breaker)
        module.reset_circuit_breaker()

        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.text = "Premium Query Parameter: this endpoint is not available under your current subscription"

        with patch("src.providers.fmp_fundamentals_provider.requests.get", return_value=mock_resp) as mock_requests_get:
            from src.providers.price_provider_base import PriceProviderError

            with self.assertRaises(PriceProviderError):
                self.provider._get("ratios", "AAPL")
            self.assertIn("ratios", module._unavailable_endpoints)

            # Second call for a DIFFERENT ticker should short-circuit —
            # no new HTTP request at all.
            mock_requests_get.reset_mock()
            with self.assertRaises(PriceProviderError):
                self.provider._get("ratios", "MSFT")
            mock_requests_get.assert_not_called()

    def test_non_plan_gated_402_does_not_trip_circuit_breaker(self):
        from src.providers import fmp_fundamentals_provider as module

        self.addCleanup(module.reset_circuit_breaker)
        module.reset_circuit_breaker()

        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.text = "some other billing error unrelated to plan tier"

        with patch("src.providers.fmp_fundamentals_provider.requests.get", return_value=mock_resp):
            from src.providers.price_provider_base import PriceProviderError

            with self.assertRaises(PriceProviderError):
                self.provider._get("ratios", "AAPL")
        self.assertNotIn("ratios", module._unavailable_endpoints)


if __name__ == "__main__":
    unittest.main()
