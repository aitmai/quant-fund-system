import unittest

from src.hedge.black_scholes import PutQuote, nearest_to_target_delta, put_delta


class TestPutDelta(unittest.TestCase):
    def test_atm_put_delta_is_roughly_negative_half(self):
        # At-the-money put with modest vol/short-ish expiry should sit
        # close to -0.5 delta — the textbook baseline sanity check.
        delta = put_delta(
            underlying_price=100.0, strike=100.0,
            time_to_expiration_years=30 / 365.0, implied_vol=0.20,
        )
        self.assertAlmostEqual(delta, -0.5, delta=0.05)

    def test_deep_otm_put_delta_near_zero(self):
        # Strike far below spot — tiny chance of finishing ITM, delta -> 0.
        delta = put_delta(
            underlying_price=100.0, strike=50.0,
            time_to_expiration_years=30 / 365.0, implied_vol=0.20,
        )
        self.assertLess(abs(delta), 0.05)

    def test_deep_itm_put_delta_near_negative_one(self):
        delta = put_delta(
            underlying_price=100.0, strike=150.0,
            time_to_expiration_years=30 / 365.0, implied_vol=0.20,
        )
        self.assertLess(delta, -0.95)

    def test_put_delta_always_negative_or_zero(self):
        for strike in [50, 80, 100, 120, 150]:
            delta = put_delta(
                underlying_price=100.0, strike=strike,
                time_to_expiration_years=45 / 365.0, implied_vol=0.25,
            )
            self.assertLessEqual(delta, 0.0)

    def test_degenerate_inputs_return_none_not_exception(self):
        self.assertIsNone(put_delta(0, 100, 30 / 365.0, 0.2))
        self.assertIsNone(put_delta(100, 0, 30 / 365.0, 0.2))
        self.assertIsNone(put_delta(100, 100, 0, 0.2))
        self.assertIsNone(put_delta(100, 100, 30 / 365.0, 0))
        self.assertIsNone(put_delta(100, 100, 30 / 365.0, -0.1))


class TestNearestToTargetDelta(unittest.TestCase):
    def _chain(self):
        # A spread of strikes at 35 DTE (inside the 30-45 window) around
        # a $500 underlying, plus one intentionally outside the DTE
        # window that should never be selectable no matter how close
        # its delta is.
        return [
            PutQuote(strike=430, days_to_expiration=35, implied_vol=0.18, bid=1.0, ask=1.2),
            PutQuote(strike=450, days_to_expiration=35, implied_vol=0.18, bid=2.5, ask=2.7),
            PutQuote(strike=470, days_to_expiration=35, implied_vol=0.18, bid=5.0, ask=5.4),
            PutQuote(strike=490, days_to_expiration=35, implied_vol=0.18, bid=9.0, ask=9.4),
            # Same strike as the true ~30-delta one, but outside the DTE window.
            PutQuote(strike=470, days_to_expiration=10, implied_vol=0.18, bid=2.0, ask=2.2),
        ]

    def test_selects_strike_within_dte_window_closest_to_target_delta(self):
        chosen = nearest_to_target_delta(
            self._chain(), underlying_price=500.0, target_delta=0.30, min_dte=30, max_dte=45,
        )
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.days_to_expiration, 35)  # never the 10-DTE row

    def test_empty_chain_returns_none(self):
        self.assertIsNone(nearest_to_target_delta([], underlying_price=500.0))

    def test_no_contracts_in_dte_window_returns_none(self):
        chain = [PutQuote(strike=470, days_to_expiration=10, implied_vol=0.18, bid=2.0, ask=2.2)]
        chosen = nearest_to_target_delta(chain, underlying_price=500.0, min_dte=30, max_dte=45)
        self.assertIsNone(chosen)


class TestPutQuoteMidPremium(unittest.TestCase):
    def test_mid_premium_averages_bid_ask(self):
        q = PutQuote(strike=470, days_to_expiration=35, implied_vol=0.18, bid=5.0, ask=5.4)
        self.assertAlmostEqual(q.mid_premium, 5.2)

    def test_mid_premium_none_when_bid_or_ask_missing(self):
        self.assertIsNone(PutQuote(strike=470, days_to_expiration=35, implied_vol=0.18, bid=None, ask=5.4).mid_premium)
        self.assertIsNone(PutQuote(strike=470, days_to_expiration=35, implied_vol=0.18, bid=5.0, ask=0).mid_premium)


if __name__ == "__main__":
    unittest.main()
