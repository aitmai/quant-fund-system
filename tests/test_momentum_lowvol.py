import unittest
from datetime import date, timedelta

import pandas as pd

from src.scoring.momentum_lowvol import (
    LOWVOL_MIN_ROWS,
    MOMENTUM_MIN_ROWS,
    compute_momentum_and_lowvol,
)


def _price_panel(ticker: str, prices: list, start: date = date(2024, 1, 1)) -> pd.DataFrame:
    dates = [start + timedelta(days=i) for i in range(len(prices))]
    return pd.DataFrame({"ticker": [ticker] * len(prices), "date": dates, "close": prices, "adj_close": prices, "price": prices})


class TestMomentumAndLowVol(unittest.TestCase):
    def test_momentum_computed_when_enough_history(self):
        # Flat 1.0 for a year, then double for the "12mo ago" price, halve
        # for "1mo ago" -> a known, checkable momentum value.
        prices = [1.0] * MOMENTUM_MIN_ROWS
        prices[-MOMENTUM_MIN_ROWS] = 100.0  # 12mo-ago price (index -253)
        prices[-22] = 150.0  # 1mo-ago price
        panel = _price_panel("AAA", prices)

        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(panel)

        self.assertIn("AAA", momentum)
        self.assertAlmostEqual(momentum["AAA"], (150.0 / 100.0) - 1)
        self.assertNotIn("AAA", momentum_skipped)

    def test_momentum_skipped_when_insufficient_history(self):
        panel = _price_panel("BBB", [1.0] * (MOMENTUM_MIN_ROWS - 1))
        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(panel)
        self.assertNotIn("BBB", momentum)
        self.assertIn("BBB", momentum_skipped)

    def test_lowvol_skipped_independently_of_momentum(self):
        # Enough history for low-vol (61 rows) but not momentum (253+).
        panel = _price_panel("CCC", [1.0] * LOWVOL_MIN_ROWS)
        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(panel)
        self.assertIn("CCC", momentum_skipped)
        self.assertNotIn("CCC", lowvol_skipped)
        self.assertIn("CCC", lowvol)

    def test_lowvol_higher_raw_means_lower_volatility(self):
        # Ticker A: constant price (zero vol). Ticker B: alternates
        # wildly (high vol). A's raw low-vol score should be HIGHER
        # (less negative) than B's, since -0 > -large_std.
        flat_prices = [100.0] * LOWVOL_MIN_ROWS
        volatile_prices = [100.0, 150.0] * (LOWVOL_MIN_ROWS // 2) + [100.0]

        panel = pd.concat([_price_panel("FLAT", flat_prices), _price_panel("VOL", volatile_prices)])
        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(panel)

        self.assertGreater(lowvol["FLAT"], lowvol["VOL"])
        self.assertAlmostEqual(lowvol["FLAT"], 0.0)

    def test_zero_price_at_lookback_anchor_is_skipped_not_a_crash(self):
        prices = [1.0] * MOMENTUM_MIN_ROWS
        prices[-MOMENTUM_MIN_ROWS] = 0.0  # degenerate/bad data
        panel = _price_panel("ZERO", prices)

        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(panel)
        self.assertNotIn("ZERO", momentum)
        self.assertIn("ZERO", momentum_skipped)

    def test_empty_panel_returns_empty_results(self):
        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(pd.DataFrame())
        self.assertEqual(momentum, {})
        self.assertEqual(lowvol, {})

    def test_multiple_tickers_scored_independently(self):
        prices_a = [1.0] * MOMENTUM_MIN_ROWS
        prices_a[-MOMENTUM_MIN_ROWS] = 100.0
        prices_a[-22] = 200.0
        prices_b = [1.0] * (MOMENTUM_MIN_ROWS - 1)  # insufficient

        panel = pd.concat([_price_panel("A", prices_a), _price_panel("B", prices_b)])
        momentum, lowvol, momentum_skipped, lowvol_skipped = compute_momentum_and_lowvol(panel)

        self.assertIn("A", momentum)
        self.assertIn("B", momentum_skipped)
        self.assertNotIn("B", momentum)


if __name__ == "__main__":
    unittest.main()
