import unittest
from unittest.mock import MagicMock, patch

import requests

from src.universe import index_sources


def _fake_ishares_csv(ticker_rows):
    header = (
        "Fund Name,iShares Some ETF\n"
        "Fund Holdings as of,Jul 10 2026\n"
        "Inception Date,May 15 2000\n"
        "\n"
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,CUSIP,Ticker,Exchange\n"
    )
    return header + "\n".join(ticker_rows) + "\n"


class TestIndexSources(unittest.TestCase):
    @patch("src.universe.index_sources.requests.get")
    def test_parses_ishares_csv_skipping_disclaimer_rows_and_cash_lines(self, mock_get):
        rows = [
            "AAPL,Apple Inc,Information Technology,Equity,1000000,5.0,1000000,1000,037833100,AAPL,NASDAQ",
            "MSFT,Microsoft Corp,Information Technology,Equity,900000,4.5,900000,900,594918104,MSFT,NASDAQ",
            "-,Cash,Cash,Cash,50000,0.25,50000,0,-,-,-",
            "CASH,Cash Component,Cash,Cash,10000,0.05,10000,0,-,CASH,-",
        ]
        mock_resp = MagicMock()
        mock_resp.text = _fake_ishares_csv(rows)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()

        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, {"AAPL", "MSFT"})  # cash rows excluded by Asset Class + ticker blacklist
        aapl = next(r for r in results if r["ticker"] == "AAPL")
        self.assertEqual(aapl["company_name"], "Apple Inc")
        self.assertEqual(aapl["sector"], "Information Technology")

    @patch("src.universe.index_sources.requests.get")
    def test_excludes_non_equity_asset_class_rows(self, mock_get):
        rows = [
            "AAPL,Apple Inc,Information Technology,Equity,1000000,5.0,1000000,1000,037833100,AAPL,NASDAQ",
            "ESZ6,S&P 500 EMINI FUT,,Exchange Traded Index Futures,500000,2.0,500000,10,-,ESZ6,CME",
        ]
        mock_resp = MagicMock()
        mock_resp.text = _fake_ishares_csv(rows)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_russell1000_constituents()
        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, {"AAPL"})

    @patch("src.universe.index_sources.requests.get")
    def test_missing_header_row_returns_empty_list_not_exception(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "Some,Unexpected,Format\nrow,without,header\n"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.requests.get")
    def test_network_failure_returns_empty_list_not_exception(self, mock_get):
        mock_get.side_effect = requests.RequestException("connection reset")
        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.requests.get")
    def test_uses_env_override_url_when_set(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = _fake_ishares_csv(
            ["ZZZZ,Test Co,Test Sector,Equity,1,1,1,1,-,ZZZZ,NASDAQ"]
        )
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with patch.dict("os.environ", {"IVV_HOLDINGS_CSV_URL": "https://example.com/custom-ivv.csv"}):
            index_sources.fetch_sp500_constituents()

        called_url = mock_get.call_args[0][0]
        self.assertEqual(called_url, "https://example.com/custom-ivv.csv")

    def test_no_fmp_api_key_required(self):
        # Should not raise even with no FMP_API_KEY set — this no longer
        # touches FMP at all for index sourcing.
        with patch.dict("os.environ", {}, clear=True):
            with patch("src.universe.index_sources.requests.get") as mock_get:
                mock_get.side_effect = requests.RequestException("offline")
                results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.requests.get")
    def test_sends_browser_like_user_agent(self, mock_get):
        # iShares has been observed to serve a non-CSV response to requests
        # without a browser User-Agent — this locks in that we always send one.
        mock_resp = MagicMock()
        mock_resp.text = _fake_ishares_csv(["AAPL,Apple Inc,Tech,Equity,1,1,1,1,-,AAPL,NASDAQ"])
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        index_sources.fetch_sp500_constituents()

        _, kwargs = mock_get.call_args
        self.assertIn("headers", kwargs)
        self.assertIn("User-Agent", kwargs["headers"])
        self.assertNotIn("python-requests", kwargs["headers"]["User-Agent"].lower())

    @patch("src.universe.index_sources.print")
    @patch("src.universe.index_sources.requests.get")
    def test_diagnostic_snippet_printed_when_header_row_missing(self, mock_get, mock_print):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body>Please enable JavaScript</body></html>"
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()

        self.assertEqual(results, [])
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("enable JavaScript", printed)
        self.assertIn("200", printed)


if __name__ == "__main__":
    unittest.main()
