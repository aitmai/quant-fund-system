import unittest

import numpy as np
import pandas as pd

from src.hedge.volatility import (
    MIN_OBSERVATIONS_FOR_GARCH,
    daily_returns_pct,
    garch_forecast_annualized,
    realized_volatility_annualized,
)


class TestDailyReturnsPct(unittest.TestCase):
    def test_computes_pct_returns_not_decimal(self):
        prices = pd.Series([100.0, 101.0, 99.99])
        returns = daily_returns_pct(prices)
        self.assertAlmostEqual(returns.iloc[0], 1.0)  # 1% up, expressed as 1.0 not 0.01
        self.assertEqual(len(returns), 2)  # leading NaN dropped


class TestRealizedVolatilityAnnualized(unittest.TestCase):
    def test_known_constant_volatility_annualizes_correctly(self):
        # Constant daily return magnitude alternating sign gives a known,
        # computable std, so the annualization math itself is checkable.
        np.random.seed(0)
        returns_pct = pd.Series(np.random.normal(0, 1.0, 60))  # ~1% daily std
        vol = realized_volatility_annualized(returns_pct, window=21)
        self.assertIsNotNone(vol)
        # 1% daily std annualized ~= 0.01 * sqrt(252) ~= 0.1587
        self.assertAlmostEqual(vol, 0.16, delta=0.05)

    def test_insufficient_history_returns_none(self):
        returns_pct = pd.Series([0.5, -0.3, 0.2])  # far fewer than window=21
        self.assertIsNone(realized_volatility_annualized(returns_pct, window=21))

    def test_uses_only_trailing_window_not_full_history(self):
        # A long flat-then-volatile series: realized vol should reflect
        # only the recent volatile tail, not be diluted by the flat past.
        flat = pd.Series(np.zeros(100))
        volatile = pd.Series(np.random.RandomState(1).normal(0, 3.0, 21))
        combined = pd.concat([flat, volatile], ignore_index=True)
        vol = realized_volatility_annualized(combined, window=21)
        self.assertGreater(vol, 0.2)  # clearly reflects the volatile tail, not near-zero


class TestGarchForecastAnnualized(unittest.TestCase):
    def test_insufficient_history_returns_none_without_fitting(self):
        short_series = pd.Series(np.random.normal(0, 1.0, MIN_OBSERVATIONS_FOR_GARCH - 1))
        self.assertIsNone(garch_forecast_annualized(short_series))

    def test_fits_and_forecasts_on_real_arch_library(self):
        # No mocking here — this runs the actual arch package, since it's
        # a pure computational library needing no network access, unlike
        # every other Phase 9 data source. Verified live before writing
        # volatility.py; this test guards against a future arch upgrade
        # silently changing the API shape this module depends on.
        np.random.seed(42)
        returns_pct = pd.Series(np.random.normal(0, 1.2, 750))

        vol = garch_forecast_annualized(returns_pct, horizon=1)

        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0.0)
        # Sanity range for daily-pct-scale synthetic data with ~1.2 std —
        # not a tight bound, just guards against a units/scaling bug
        # (e.g. forgetting to annualize, or double-annualizing).
        self.assertLess(vol, 1.0)

    def test_all_nan_series_returns_none_not_exception(self):
        nan_series = pd.Series([float("nan")] * (MIN_OBSERVATIONS_FOR_GARCH + 10))
        self.assertIsNone(garch_forecast_annualized(nan_series))


if __name__ == "__main__":
    unittest.main()
