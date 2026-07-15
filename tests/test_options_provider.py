import math
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd

from src.providers.options_provider import OptionsProviderError, select_spy_hedge_put


def _mock_puts_df(rows):
    """rows: list of dicts with strike, bid, ask, impliedVolatility,
    contractSize (defaults to REGULAR if omitted) — mirrors the real
    columns confirmed from a live yfinance chain."""
    for r in rows:
        r.setdefault("contractSize", "REGULAR")
    return pd.DataFrame(rows)


class TestSelectSpyHedgePut(unittest.TestCase):
    def _mock_ticker(self, mock_ticker_cls, expirations, chains_by_expiration, price=751.83):
        mock_ticker = MagicMock()
        mock_ticker.options = expirations

        def option_chain(exp_str):
            chain = MagicMock()
            chain.puts = chains_by_expiration[exp_str]
            return chain

        mock_ticker.option_chain.side_effect = option_chain

        mock_hist = pd.DataFrame({"Close": [price]})
        mock_ticker.history.return_value = mock_hist

        mock_ticker_cls.return_value = mock_ticker
        return mock_ticker

    @patch("src.providers.options_provider.yf.Ticker")
    def test_selects_strike_closest_to_target_delta_within_window(self, mock_ticker_cls):
        as_of = date(2026, 7, 14)
        exp_in_window = "2026-08-14"  # 31 DTE from as_of
        exp_out_of_window = "2026-07-20"  # 6 DTE — must never be selectable

        puts = _mock_puts_df(
            [
                {"strike": 690.0, "bid": 1.67, "ask": 1.69, "impliedVolatility": 0.2126},
                {"strike": 736.0, "bid": 6.34, "ask": 6.38, "impliedVolatility": 0.1476},  # ~30-delta, confirmed live
                {"strike": 750.0, "bid": 10.08, "ask": 10.13, "impliedVolatility": 0.1263},
            ]
        )
        out_of_window_puts = _mock_puts_df(
            [{"strike": 736.0, "bid": 3.0, "ask": 3.1, "impliedVolatility": 0.20}]
        )

        self._mock_ticker(
            mock_ticker_cls,
            expirations=(exp_out_of_window, exp_in_window),
            chains_by_expiration={exp_in_window: puts, exp_out_of_window: out_of_window_puts},
        )

        result = select_spy_hedge_put(as_of=as_of)

        self.assertEqual(result.strike, 736.0)
        self.assertEqual(result.days_to_expiration, 31)
        self.assertAlmostEqual(abs(result.delta), 0.30, delta=0.02)
        self.assertAlmostEqual(result.premium, 6.36)
        self.assertAlmostEqual(result.implied_vol, 0.1476)

    @patch("src.providers.options_provider.yf.Ticker")
    def test_no_expirations_in_window_raises(self, mock_ticker_cls):
        as_of = date(2026, 7, 14)
        self._mock_ticker(
            mock_ticker_cls,
            expirations=("2026-07-20",),  # only 6 DTE — nothing in 30-45 window
            chains_by_expiration={},
        )
        with self.assertRaises(OptionsProviderError):
            select_spy_hedge_put(as_of=as_of)

    @patch("src.providers.options_provider.yf.Ticker")
    def test_nan_bid_ask_does_not_crash_and_is_excluded_from_premium(self, mock_ticker_cls):
        # Regression test for the real live-chain quirk: NaN (not just 0)
        # bid/ask on thin strikes — confirmed 2026-07-14 on SPY260814 puts.
        as_of = date(2026, 7, 14)
        exp = "2026-08-14"
        puts = _mock_puts_df(
            [
                {"strike": 736.0, "bid": math.nan, "ask": 6.38, "impliedVolatility": 0.1476},
                {"strike": 750.0, "bid": 10.08, "ask": 10.13, "impliedVolatility": 0.1263},
            ]
        )
        self._mock_ticker(
            mock_ticker_cls, expirations=(exp,), chains_by_expiration={exp: puts},
        )

        # 736's NaN bid makes its premium unusable, so 750 (further from
        # target delta but with a valid premium) should be selected instead
        # of raising or silently producing a bad premium.
        result = select_spy_hedge_put(as_of=as_of)
        self.assertEqual(result.strike, 750.0)
        self.assertIsNotNone(result.premium)

    @patch("src.providers.options_provider.yf.Ticker")
    def test_nonstandard_contracts_are_skipped(self, mock_ticker_cls):
        as_of = date(2026, 7, 14)
        exp = "2026-08-14"
        puts = _mock_puts_df(
            [
                {"strike": 736.0, "bid": 6.34, "ask": 6.38, "impliedVolatility": 0.1476,
                 "contractSize": "NonStandard"},
                {"strike": 750.0, "bid": 10.08, "ask": 10.13, "impliedVolatility": 0.1263},
            ]
        )
        self._mock_ticker(
            mock_ticker_cls, expirations=(exp,), chains_by_expiration={exp: puts},
        )

        result = select_spy_hedge_put(as_of=as_of)
        self.assertEqual(result.strike, 750.0)  # the NonStandard 736 row must be excluded

    @patch("src.providers.options_provider.yf.Ticker")
    def test_all_quotes_unusable_raises_rather_than_returning_none(self, mock_ticker_cls):
        as_of = date(2026, 7, 14)
        exp = "2026-08-14"
        puts = _mock_puts_df(
            [{"strike": 736.0, "bid": math.nan, "ask": math.nan, "impliedVolatility": 0.1476}]
        )
        self._mock_ticker(
            mock_ticker_cls, expirations=(exp,), chains_by_expiration={exp: puts},
        )
        with self.assertRaises(OptionsProviderError):
            select_spy_hedge_put(as_of=as_of)


if __name__ == "__main__":
    unittest.main()
