import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers.yfinance_provider import REQUEST_TIMEOUT_SECONDS, YFinanceProvider


class TestYFinanceTimeout(unittest.TestCase):
    @patch("src.providers.yfinance_provider.yf.Ticker")
    def test_passes_explicit_timeout_to_history_call(self, mock_ticker_cls):
        mock_df = MagicMock()
        mock_df.empty = False
        mock_df.iterrows.return_value = iter([])
        mock_ticker_cls.return_value.history.return_value = mock_df

        provider = YFinanceProvider()
        provider.fetch_history("AAPL", date(2026, 1, 1), date(2026, 1, 5))

        _, kwargs = mock_ticker_cls.return_value.history.call_args
        self.assertIn("timeout", kwargs)
        self.assertEqual(kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)

    @patch("src.providers.yfinance_provider.yf.Ticker")
    def test_timeout_exception_is_retryable(self, mock_ticker_cls):
        import requests

        mock_ticker_cls.return_value.history.side_effect = requests.exceptions.Timeout("stalled")

        provider = YFinanceProvider()
        from src.providers.price_provider_base import PriceProviderError

        with self.assertRaises(PriceProviderError) as ctx:
            provider.fetch_history("AAPL", date(2026, 1, 1), date(2026, 1, 5))
        self.assertTrue(ctx.exception.retryable)


if __name__ == "__main__":
    unittest.main()
