import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers import provider_factory
from src.providers.price_provider_base import PriceBar, PriceProviderError


def _fake_conn(active_provider="yfinance"):
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchone.return_value = (active_provider,)
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn


class TestProviderFallback(unittest.TestCase):
    # Patch PROVIDER_CLASSES entries directly rather than the imported names —
    # provider_factory.py looks classes up via that dict, built once at import
    # time, so patching the module-level names it was built from has no effect.

    def test_active_provider_succeeds_no_fallback_needed(self):
        conn = _fake_conn(active_provider="yfinance")
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.return_value = [PriceBar("AAPL", date(2026, 1, 2), 1, 2, 0.5, 1.5, 1.5, 1000)]
        mock_tiingo_cls = MagicMock()

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {"yfinance": MagicMock(return_value=mock_yf_instance), "tiingo": mock_tiingo_cls},
        ):
            bars, provider_used = provider_factory.fetch_with_fallback(conn, "AAPL", date(2026, 1, 1))

        self.assertEqual(provider_used, "yfinance")
        self.assertEqual(len(bars), 1)
        mock_tiingo_cls.assert_not_called()

    def test_falls_back_when_active_provider_fails(self):
        conn = _fake_conn(active_provider="yfinance")
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.side_effect = PriceProviderError("throttled", retryable=True)

        mock_tiingo_instance = MagicMock()
        mock_tiingo_instance.fetch_history.return_value = [
            PriceBar("AAPL", date(2026, 1, 2), 1, 2, 0.5, 1.5, 1.5, 1000)
        ]

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {
                "yfinance": MagicMock(return_value=mock_yf_instance),
                "tiingo": MagicMock(return_value=mock_tiingo_instance),
            },
        ):
            bars, provider_used = provider_factory.fetch_with_fallback(conn, "AAPL", date(2026, 1, 1))

        self.assertEqual(provider_used, "tiingo")
        self.assertEqual(len(bars), 1)

    def test_raises_when_both_providers_fail(self):
        conn = _fake_conn(active_provider="yfinance")
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.side_effect = PriceProviderError("no data", retryable=False)

        mock_tiingo_instance = MagicMock()
        mock_tiingo_instance.fetch_history.side_effect = PriceProviderError("404", retryable=False)

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {
                "yfinance": MagicMock(return_value=mock_yf_instance),
                "tiingo": MagicMock(return_value=mock_tiingo_instance),
            },
        ):
            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "BADTICKER", date(2026, 1, 1))

    def test_active_provider_tiingo(self):
        conn = _fake_conn(active_provider="tiingo")
        mock_tiingo_instance = MagicMock()
        mock_tiingo_instance.fetch_history.return_value = [
            PriceBar("MSFT", date(2026, 1, 2), 1, 2, 0.5, 1.5, 1.5, 1000)
        ]

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {"tiingo": MagicMock(return_value=mock_tiingo_instance), "yfinance": MagicMock()},
        ):
            bars, provider_used = provider_factory.fetch_with_fallback(conn, "MSFT", date(2026, 1, 1))
        self.assertEqual(provider_used, "tiingo")


if __name__ == "__main__":
    unittest.main()
