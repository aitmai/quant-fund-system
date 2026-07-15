import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers.vix_futures_provider import (
    VixFuturesProviderError,
    VixFuturesQuote,
    fetch_contract_settle,
    fetch_term_structure,
)

# Real CSV content confirmed live 2026-07-15 from
# https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2027-01-20.csv
REAL_CSV_SAMPLE = """Trade Date,Futures,Open,High,Low,Close,Settle,Change,Total Volume,EFP,Open Interest
2026-04-20,F (Jan 2027),0.0000,22.3000,23.5000,0.0000,22.90,0,0,0,0
2026-04-24,F (Jan 2027),23.2000,23.2500,23.2000,23.2500,23.25,0.225,15,0,13
2026-05-04,F (Jan 2027),22.9500,23.1000,22.9500,22.9500,23.125,0.175,2,0,32
2026-05-05,F (Jan 2027),23.0000,23.2500,23.0000,23.1000,23.275,0.150,3,0,35
"""


def _mock_response(text, status_ok=True):
    resp = MagicMock()
    resp.text = text
    if status_ok:
        resp.raise_for_status.return_value = None
    else:
        import requests
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError("404")
    return resp


class TestFetchContractSettle(unittest.TestCase):
    @patch("src.providers.vix_futures_provider.requests.get")
    def test_returns_most_recent_row_using_settle_not_close(self, mock_get):
        # Real-data quirk regression: first row has Close=0.0000 but a
        # real Settle=22.90 — must never be mistaken for "no price."
        mock_get.return_value = _mock_response(REAL_CSV_SAMPLE)

        quote = fetch_contract_settle(date(2027, 1, 20))

        self.assertEqual(quote.trade_date, date(2026, 5, 5))
        self.assertAlmostEqual(quote.settle, 23.275)
        self.assertEqual(quote.open_interest, 35)

    @patch("src.providers.vix_futures_provider.requests.get")
    def test_correct_url_constructed_from_expiration_date(self, mock_get):
        mock_get.return_value = _mock_response(REAL_CSV_SAMPLE)
        fetch_contract_settle(date(2027, 1, 20))
        called_url = mock_get.call_args[0][0]
        self.assertEqual(
            called_url,
            "https://cdn.cboe.com/data/us/futures/market_statistics/historical_data/VX/VX_2027-01-20.csv",
        )

    @patch("src.providers.vix_futures_provider.requests.get")
    def test_http_error_raises_provider_error(self, mock_get):
        mock_get.return_value = _mock_response("", status_ok=False)
        with self.assertRaises(VixFuturesProviderError):
            fetch_contract_settle(date(2027, 1, 20))

    @patch("src.providers.vix_futures_provider.requests.get")
    def test_empty_csv_raises_provider_error_not_silent_failure(self, mock_get):
        header_only = "Trade Date,Futures,Open,High,Low,Close,Settle,Change,Total Volume,EFP,Open Interest\n"
        mock_get.return_value = _mock_response(header_only)
        with self.assertRaises(VixFuturesProviderError):
            fetch_contract_settle(date(2027, 1, 20))


class TestFetchTermStructure(unittest.TestCase):
    @patch("src.providers.vix_futures_provider.fetch_contract_settle")
    @patch("src.providers.vix_futures_provider.front_and_next_month_contracts")
    def test_contango_when_next_month_priced_higher(self, mock_contracts, mock_fetch):
        mock_contracts.return_value = (date(2026, 8, 19), date(2026, 9, 16))
        mock_fetch.side_effect = [
            VixFuturesQuote(date(2026, 8, 19), date(2026, 7, 14), settle=17.0, open_interest=100),
            VixFuturesQuote(date(2026, 9, 16), date(2026, 7, 14), settle=19.0, open_interest=80),
        ]

        snapshot = fetch_term_structure(as_of=date(2026, 7, 14))

        self.assertTrue(snapshot.is_contango)
        self.assertEqual(snapshot.term_structure_signal, "contango")
        self.assertAlmostEqual(snapshot.roll_yield_pct, (19.0 - 17.0) / 17.0 * 100.0)

    @patch("src.providers.vix_futures_provider.fetch_contract_settle")
    @patch("src.providers.vix_futures_provider.front_and_next_month_contracts")
    def test_backwardation_when_front_month_priced_higher(self, mock_contracts, mock_fetch):
        mock_contracts.return_value = (date(2026, 8, 19), date(2026, 9, 16))
        mock_fetch.side_effect = [
            VixFuturesQuote(date(2026, 8, 19), date(2026, 7, 14), settle=28.0, open_interest=100),
            VixFuturesQuote(date(2026, 9, 16), date(2026, 7, 14), settle=24.0, open_interest=80),
        ]

        snapshot = fetch_term_structure(as_of=date(2026, 7, 14))

        self.assertFalse(snapshot.is_contango)
        self.assertEqual(snapshot.term_structure_signal, "backwardation")


if __name__ == "__main__":
    unittest.main()
