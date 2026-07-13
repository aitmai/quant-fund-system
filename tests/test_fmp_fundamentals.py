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
    def test_no_data_returns_empty_list(self, mock_get):
        mock_get.side_effect = lambda path, ticker: []
        rows = self.provider.fetch_fundamentals("TEST")
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
