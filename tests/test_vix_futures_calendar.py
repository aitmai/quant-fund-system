import unittest
from datetime import date

from src.hedge.vix_futures_calendar import (
    effective_sizing_contract,
    front_and_next_month_contracts,
    third_friday,
    vix_futures_expiration,
)


class TestThirdFriday(unittest.TestCase):
    def test_known_third_fridays(self):
        # Spot-checked against a calendar.
        self.assertEqual(third_friday(2026, 8), date(2026, 8, 21))
        self.assertEqual(third_friday(2027, 1), date(2027, 1, 15))


class TestVixFuturesExpiration(unittest.TestCase):
    def test_matches_real_cboe_url_confirmed_2026_07_15(self):
        # VX_2027-01-20.csv is a real, live CBOE URL for the Jan 2027
        # contract — ground truth for this whole module.
        self.assertEqual(vix_futures_expiration(2027, 1), date(2027, 1, 20))

    def test_expiration_is_always_a_wednesday(self):
        for year in (2026, 2027):
            for month in range(1, 13):
                exp = vix_futures_expiration(year, month)
                self.assertEqual(exp.weekday(), 2, f"{year}-{month} expiration {exp} not a Wednesday")

    def test_expiration_falls_within_or_near_the_labeled_month(self):
        # The labeled month's contract should expire in that same month
        # (CBOE's convention), not drift into an adjacent month.
        exp = vix_futures_expiration(2026, 8)
        self.assertEqual(exp.month, 8)
        self.assertEqual(exp.year, 2026)


class TestFrontAndNextMonthContracts(unittest.TestCase):
    def test_returns_two_ascending_unexpired_dates(self):
        as_of = date(2026, 7, 14)
        front, nxt = front_and_next_month_contracts(as_of)
        self.assertGreaterEqual(front, as_of)
        self.assertGreater(nxt, front)

    def test_does_not_skip_a_contract_expiring_later_in_the_current_month(self):
        # Pick an as_of date right before a known expiration (Aug 2026 =
        # 2026-08-19, a Wednesday) to confirm that contract isn't skipped
        # just because as_of is already in August.
        aug_2026_expiration = vix_futures_expiration(2026, 8)
        as_of = aug_2026_expiration - date.resolution  # one day before expiry
        front, _ = front_and_next_month_contracts(as_of)
        self.assertEqual(front, aug_2026_expiration)

    def test_already_expired_front_month_is_excluded(self):
        # The day AFTER a contract's expiration, that contract must not
        # be returned as the front month anymore.
        aug_2026_expiration = vix_futures_expiration(2026, 8)
        as_of = aug_2026_expiration + date.resolution
        front, nxt = front_and_next_month_contracts(as_of)
        self.assertGreater(front, aug_2026_expiration)
        self.assertGreater(nxt, front)


class TestEffectiveSizingContract(unittest.TestCase):
    def test_no_roll_when_outside_window(self):
        # Confirmed real scenario: 2026-07-14 is 6 TRADING days before
        # the 2026-07-22 front-month expiry — outside the 5-day window.
        as_of = date(2026, 7, 14)
        front = vix_futures_expiration(2026, 7)
        contract, rolled = effective_sizing_contract(as_of, roll_window_trading_days=5)
        self.assertEqual(contract, front)
        self.assertFalse(rolled)

    def test_rolls_to_next_month_inside_window(self):
        as_of = date(2026, 7, 15)  # 5 trading days before 2026-07-22 expiry
        front = vix_futures_expiration(2026, 7)
        next_month = vix_futures_expiration(2026, 8)
        contract, rolled = effective_sizing_contract(as_of, roll_window_trading_days=5)
        self.assertEqual(contract, next_month)
        self.assertTrue(rolled)
        self.assertNotEqual(contract, front)

    def test_far_from_expiry_never_rolls(self):
        as_of = date(2026, 7, 1)
        front = vix_futures_expiration(2026, 7)
        contract, rolled = effective_sizing_contract(as_of, roll_window_trading_days=5)
        self.assertEqual(contract, front)
        self.assertFalse(rolled)


if __name__ == "__main__":
    unittest.main()
