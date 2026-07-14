import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers import provider_factory
from src.providers.price_provider_base import AllProvidersUnavailableError, PriceBar, PriceProviderError


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

    def setUp(self):
        provider_factory.reset_circuit_breaker()

    def tearDown(self):
        provider_factory.reset_circuit_breaker()

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

    def test_rate_limited_provider_is_skipped_on_subsequent_tickers(self):
        # First ticker: yfinance fails (not rate-limit-flavored), Tiingo
        # fails with a rate-limit error -> Tiingo should be circuit-broken.
        conn = _fake_conn(active_provider="yfinance")
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.side_effect = PriceProviderError("throttled", retryable=True)
        mock_tiingo_instance = MagicMock()
        mock_tiingo_instance.fetch_history.side_effect = PriceProviderError(
            "Tiingo rate limit hit fetching X", retryable=True
        )

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {
                "yfinance": MagicMock(return_value=mock_yf_instance),
                "tiingo": MagicMock(return_value=mock_tiingo_instance),
            },
        ):
            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "FIRST", date(2026, 1, 1))

            self.assertIn("tiingo", provider_factory._rate_limited_providers)

            # Second ticker: yfinance fails again, but Tiingo should be
            # skipped entirely this time — never called again this run.
            mock_yf_instance.fetch_history.side_effect = PriceProviderError("throttled", retryable=True)
            mock_tiingo_instance.fetch_history.reset_mock(side_effect=True)
            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "SECOND", date(2026, 1, 1))

            mock_tiingo_instance.fetch_history.assert_not_called()

    def test_non_rate_limit_error_does_not_trip_circuit_breaker(self):
        conn = _fake_conn(active_provider="tiingo")
        mock_tiingo_instance = MagicMock()
        mock_tiingo_instance.fetch_history.side_effect = PriceProviderError(
            "Tiingo has no data for BADTICKER (404)", retryable=False
        )
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.return_value = [
            PriceBar("X", date(2026, 1, 2), 1, 2, 0.5, 1.5, 1.5, 1000)
        ]

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {
                "tiingo": MagicMock(return_value=mock_tiingo_instance),
                "yfinance": MagicMock(return_value=mock_yf_instance),
            },
        ):
            provider_factory.fetch_with_fallback(conn, "BADTICKER", date(2026, 1, 1))

        self.assertNotIn("tiingo", provider_factory._rate_limited_providers)

    def test_reset_circuit_breaker_clears_state(self):
        provider_factory._rate_limited_providers.add("tiingo")
        provider_factory.reset_circuit_breaker()
        self.assertEqual(provider_factory._rate_limited_providers, set())

    def test_repeated_ambiguous_failures_trip_circuit_breaker(self):
        # Confirmed live 2026-07-14: yfinance's "Yahoo silently blocked us"
        # failure never says "rate limit" — it looks identical, per call,
        # to a genuinely bad/delisted ticker. PROVIDER_FAILURE_CIRCUIT_THRESHOLD
        # consecutive failures across DIFFERENT tickers should trip the
        # breaker anyway, since real delistings don't cluster like that.
        conn = _fake_conn(active_provider="tiingo")
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.side_effect = PriceProviderError(
            "yfinance returned no rows for X (delisted, bad symbol, or throttled)",
            retryable=True,
        )
        mock_tiingo_instance = MagicMock()
        mock_tiingo_instance.fetch_history.side_effect = PriceProviderError(
            "Tiingo has no data (404)", retryable=False
        )

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {
                "tiingo": MagicMock(return_value=mock_tiingo_instance),
                "yfinance": MagicMock(return_value=mock_yf_instance),
            },
        ):
            self.assertNotIn("yfinance", provider_factory._rate_limited_providers)

            for i in range(provider_factory.PROVIDER_FAILURE_CIRCUIT_THRESHOLD):
                with self.assertRaises(PriceProviderError):
                    provider_factory.fetch_with_fallback(conn, f"TICK{i}", date(2026, 1, 1))

            # After exactly the threshold's worth of consecutive failures,
            # yfinance should now be circuit-broken even though it never
            # said "rate limit".
            self.assertIn("yfinance", provider_factory._rate_limited_providers)

    def test_all_providers_unavailable_raises_distinct_error_without_new_http_calls(self):
        conn = _fake_conn(active_provider="tiingo")
        provider_factory._rate_limited_providers.add("tiingo")
        provider_factory._rate_limited_providers.add("yfinance")

        mock_tiingo_cls = MagicMock()
        mock_yf_cls = MagicMock()

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {"tiingo": mock_tiingo_cls, "yfinance": mock_yf_cls},
        ):
            with self.assertRaises(AllProvidersUnavailableError):
                provider_factory.fetch_with_fallback(conn, "ANYTICK", date(2026, 1, 1))

        # Both providers were already circuit-broken, so neither should
        # even have been instantiated for this call — no wasted HTTP call.
        mock_tiingo_cls.assert_not_called()
        mock_yf_cls.assert_not_called()

    def test_success_resets_consecutive_failure_counter(self):
        conn = _fake_conn(active_provider="tiingo")
        mock_tiingo_instance = MagicMock()
        # Two failures, then a success, then two more failures — should
        # need a FULL new streak of PROVIDER_FAILURE_CIRCUIT_THRESHOLD
        # after the success before tripping, not just a running total.
        mock_tiingo_instance.fetch_history.side_effect = [
            PriceProviderError("no data", retryable=False),
            PriceProviderError("no data", retryable=False),
            [PriceBar("GOOD", date(2026, 1, 2), 1, 1, 1, 1, 1, 100)],
            PriceProviderError("no data", retryable=False),
            PriceProviderError("no data", retryable=False),
        ]
        mock_yf_instance = MagicMock()
        mock_yf_instance.fetch_history.side_effect = PriceProviderError(
            "yfinance returned no rows (delisted, bad symbol, or throttled)", retryable=True
        )

        with patch.dict(
            provider_factory.PROVIDER_CLASSES,
            {
                "tiingo": MagicMock(return_value=mock_tiingo_instance),
                "yfinance": MagicMock(return_value=mock_yf_instance),
            },
        ):
            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "A", date(2026, 1, 1))
            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "B", date(2026, 1, 1))
            # Success in between — counter should reset to 0.
            provider_factory.fetch_with_fallback(conn, "GOOD", date(2026, 1, 1))
            self.assertNotIn("tiingo", provider_factory._rate_limited_providers)

            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "C", date(2026, 1, 1))
            with self.assertRaises(PriceProviderError):
                provider_factory.fetch_with_fallback(conn, "D", date(2026, 1, 1))
            # Only 2 failures since the reset (threshold is 3) — still not tripped.
            self.assertNotIn("tiingo", provider_factory._rate_limited_providers)


if __name__ == "__main__":
    unittest.main()
