import unittest
from unittest.mock import MagicMock, patch

import requests

from src.universe import index_sources


def _fake_wikipedia_html(rows_html):
    return f"""
    <html><body>
    <table class="wikitable sortable" id="constituents">
    <tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th>
        <th>Headquarters Location</th><th>Date added</th><th>CIK</th></tr>
    {rows_html}
    </table>
    </body></html>
    """


def _fake_ishares_csv(ticker_rows):
    header = (
        "Fund Name,iShares Some ETF\n"
        "Fund Holdings as of,Jul 10 2026\n"
        "Inception Date,May 15 2000\n"
        "\n"
        "Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Shares,CUSIP,Ticker,Exchange\n"
    )
    return header + "\n".join(ticker_rows) + "\n"


class TestSp500WikipediaSource(unittest.TestCase):
    @patch("src.universe.index_sources.requests.get")
    def test_extracts_and_pads_cik(self, mock_get):
        rows_html = """
        <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td>
            <td>x</td><td>x</td><td>x</td><td>320193</td></tr>
        """
        mock_resp = MagicMock()
        mock_resp.text = _fake_wikipedia_html(rows_html)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        # Real Wikipedia CIKs already come zero-padded (e.g. "0000320193"),
        # but pad defensively regardless of what's in the cell.
        self.assertEqual(results[0]["cik"], "0000320193")

    @patch("src.universe.index_sources.requests.get")
    def test_already_padded_cik_stays_padded(self, mock_get):
        rows_html = """
        <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td>
            <td>x</td><td>x</td><td>x</td><td>0000320193</td></tr>
        """
        mock_resp = MagicMock()
        mock_resp.text = _fake_wikipedia_html(rows_html)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results[0]["cik"], "0000320193")

    @patch("src.universe.index_sources.requests.get")
    def test_missing_cik_column_gives_none_not_error(self, mock_get):
        # Table without a CIK column at all — shouldn't blow up.
        html = """
        <html><body>
        <table class="wikitable sortable" id="constituents">
        <tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr>
        <tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td></tr>
        </table>
        </body></html>
        """
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertIsNone(results[0]["cik"])


    @patch("src.universe.index_sources.requests.get")
    def test_parses_constituents_table(self, mock_get):
        rows_html = """
        <tr><td>AAPL</td><td>Apple Inc.</td><td>Information Technology</td>
            <td>Technology Hardware</td><td>Cupertino, California</td><td>1982-11-30</td><td>320193</td></tr>
        <tr><td>MSFT</td><td>Microsoft Corp.</td><td>Information Technology</td>
            <td>Systems Software</td><td>Redmond, Washington</td><td>1994-06-01</td><td>789019</td></tr>
        """
        mock_resp = MagicMock()
        mock_resp.text = _fake_wikipedia_html(rows_html)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()

        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, {"AAPL", "MSFT"})
        aapl = next(r for r in results if r["ticker"] == "AAPL")
        self.assertEqual(aapl["company_name"], "Apple Inc.")
        self.assertEqual(aapl["sector"], "Information Technology")

    @patch("src.universe.index_sources.requests.get")
    def test_normalizes_dot_tickers_to_hyphen(self, mock_get):
        rows_html = '<tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td><td>x</td><td>x</td><td>x</td><td>1067983</td></tr>'
        mock_resp = MagicMock()
        mock_resp.text = _fake_wikipedia_html(rows_html)
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results[0]["ticker"], "BRK-B")

    @patch("src.universe.index_sources.requests.get")
    def test_network_failure_returns_empty_list_not_exception(self, mock_get):
        mock_get.side_effect = requests.RequestException("connection reset")
        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.requests.get")
    def test_missing_table_returns_empty_list(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body>no table here</body></html>"
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.requests.get")
    def test_column_header_variant_ticker_symbol_still_matches(self, mock_get):
        html = """
        <html><body>
        <table class="wikitable sortable" id="constituents">
        <tr><th>Ticker symbol</th><th>Security</th><th>GICS Sector</th></tr>
        <tr><td>GOOGL</td><td>Alphabet Inc.</td><td>Communication Services</td></tr>
        </table>
        </body></html>
        """
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results[0]["ticker"], "GOOGL")

    @patch("src.universe.index_sources.requests.get")
    def test_sends_identifiable_wikipedia_user_agent(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = _fake_wikipedia_html(
            '<tr><td>AAPL</td><td>Apple Inc.</td><td>Tech</td><td>x</td><td>x</td><td>x</td><td>1</td></tr>'
        )
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        index_sources.fetch_sp500_constituents()
        _, kwargs = mock_get.call_args
        self.assertIn("User-Agent", kwargs["headers"])
        self.assertIn("quant-fund-system", kwargs["headers"]["User-Agent"])

    @patch("src.universe.index_sources.requests.get")
    def test_no_fmp_dependency(self, mock_get):
        with patch.dict("os.environ", {}, clear=True):
            mock_get.side_effect = requests.RequestException("offline")
            results = index_sources.fetch_sp500_constituents()
        self.assertEqual(results, [])


class TestRussell1000IsharesSource(unittest.TestCase):
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

        results = index_sources.fetch_russell1000_constituents()
        tickers = {r["ticker"] for r in results}
        self.assertEqual(tickers, {"AAPL", "MSFT"})

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
    def test_network_failure_returns_empty_list_not_exception(self, mock_get):
        mock_get.side_effect = requests.RequestException("connection reset")
        results = index_sources.fetch_russell1000_constituents()
        self.assertEqual(results, [])

    @patch("src.universe.index_sources.print")
    @patch("src.universe.index_sources.requests.get")
    def test_diagnostic_snippet_printed_when_blocked(self, mock_get, mock_print):
        mock_resp = MagicMock()
        mock_resp.text = "<html><body>Please enable JavaScript</body></html>"
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "text/csv"}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        results = index_sources.fetch_russell1000_constituents()
        self.assertEqual(results, [])
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("enable JavaScript", printed)

    @patch("src.universe.index_sources.requests.get")
    def test_uses_env_override_url_when_set(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.text = _fake_ishares_csv(["ZZZZ,Test Co,Test Sector,Equity,1,1,1,1,-,ZZZZ,NASDAQ"])
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with patch.dict("os.environ", {"IWB_HOLDINGS_CSV_URL": "https://example.com/custom-iwb.csv"}):
            index_sources.fetch_russell1000_constituents()

        called_url = mock_get.call_args[0][0]
        self.assertEqual(called_url, "https://example.com/custom-iwb.csv")


if __name__ == "__main__":
    unittest.main()
