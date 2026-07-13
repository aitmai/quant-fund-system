import unittest
from unittest.mock import MagicMock, patch

import requests

from src.universe import index_sources


def _fake_iwb_csv():
    # Mimics the real iShares CSV layout: several disclaimer/header rows
    # before the actual holdings table starts.
    return (
        "Fund Name,iShares Russell 1000 ETF\n"
        "Fund Holdings as of,Jul 10 2026\n"
        "Inception Date,May 15 2000\n"
        "\n"
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,CUSIP,Ticker,Exchange\n"
        "AAPL,Apple Inc,Information Technology,Equity,1000000,5.0,1000000,1000,037833100,AAPL,NASDAQ\n"
        "MSFT,Microsoft Corp,Information Technology,Equity,900000,4.5,900000,900,594918104,MSFT,NASDAQ\n"
        "-,Cash,Cash,Cash,50000,0.25,50000,0,-,-,-\n"
        "CASH,Cash Component,Cash,Cash,10000,0.05,10000,0,-,CASH,-\n"
    )


class TestIndexSources(unittest.TestCase):
    @patch("src.universe.index_sources.requests.get")
    def test_parses_iwb_csv_skipping_disclaimer_rows_and_cash_lines(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = _fake_iwb_csv()
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_russell1000_constituents()

        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, {"AAPL", "MSFT"})  # cash rows excluded
        aapl = next(r for r in results if r["ticker"] == "AAPL")
        self.assertEqual(aapl["company_name"], "Apple Inc")
        self.assertEqual(aapl["sector"], "Information Technology")

    @patch("src.universe.index_sources.requests.get")
    def test_missing_header_row_returns_empty_list_not_exception(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "Some,Unexpected,Format\nrow,without,header\n"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_russell1000_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.requests.get")
    def test_network_failure_returns_empty_list_not_exception(self, mock_get):
        mock_get.side_effect = requests.RequestException("connection reset")
        results = index_sources.fetch_russell1000_constituents()
        self.assertEqual(results, [])

    @patch.dict("os.environ", {"FMP_API_KEY": "fake-key"})
    @patch("src.universe.index_sources.requests.get")
    def test_sp500_constituents_parses_fmp_response(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"symbol": "AAPL", "name": "Apple Inc", "sector": "Technology"},
            {"symbol": "", "name": "should be skipped, no symbol"},
        ]
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ticker"], "AAPL")

    @patch.dict("os.environ", {}, clear=True)
    def test_sp500_raises_without_api_key(self):
        with self.assertRaises(RuntimeError):
            index_sources.fetch_sp500_constituents()


if __name__ == "__main__":
    unittest.main()
