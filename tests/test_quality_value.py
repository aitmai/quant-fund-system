import unittest
from datetime import date

import pandas as pd

from src.scoring.quality_value import compute_quality_value_raw


def _fundamentals_row(ticker, roe=None, debt_equity=None, earnings_variance=None, fcf_yield=None, ev_ebitda=None):
    return {
        "ticker": ticker,
        "report_date": date(2025, 12, 31),
        "filed_date": date(2026, 2, 1),
        "roe": roe,
        "ev_ebitda": ev_ebitda,
        "fcf_yield": fcf_yield,
        "debt_equity": debt_equity,
        "earnings_variance": earnings_variance,
    }


class TestQualityValueRaw(unittest.TestCase):
    def test_all_metrics_present(self):
        df = pd.DataFrame([_fundamentals_row("AAA", roe=0.2, debt_equity=0.5, earnings_variance=0.1, fcf_yield=0.05, ev_ebitda=12.0)])
        quality, value = compute_quality_value_raw(df)

        self.assertEqual(quality["roe"]["AAA"], 0.2)
        self.assertEqual(value["fcf_yield"]["AAA"], 0.05)

    def test_debt_equity_sign_is_inverted(self):
        # Higher debt/equity is WORSE, so the raw sub-metric should be negative.
        df = pd.DataFrame([_fundamentals_row("AAA", debt_equity=0.5)])
        quality, _ = compute_quality_value_raw(df)
        self.assertEqual(quality["debt_equity_inv"]["AAA"], -0.5)

    def test_earnings_variance_sign_is_inverted(self):
        df = pd.DataFrame([_fundamentals_row("AAA", earnings_variance=0.3)])
        quality, _ = compute_quality_value_raw(df)
        self.assertEqual(quality["earnings_stability"]["AAA"], -0.3)

    def test_ev_ebitda_sign_is_inverted(self):
        # Lower EV/EBITDA (cheaper) is BETTER, so raw should be negative of it.
        df = pd.DataFrame([_fundamentals_row("AAA", ev_ebitda=15.0)])
        _, value = compute_quality_value_raw(df)
        self.assertEqual(value["ev_ebitda_inv"]["AAA"], -15.0)

    def test_missing_submetric_is_absent_not_none(self):
        # A missing value should mean the ticker key doesn't exist in that
        # sub-dict at all — not present with a None/NaN value — so callers
        # can distinguish "missing" from "zero" with a plain `in` check.
        df = pd.DataFrame([_fundamentals_row("AAA", roe=0.2, debt_equity=None)])
        quality, _ = compute_quality_value_raw(df)
        self.assertIn("AAA", quality["roe"])
        self.assertNotIn("AAA", quality["debt_equity_inv"])

    def test_empty_panel_returns_empty_dicts(self):
        quality, value = compute_quality_value_raw(pd.DataFrame())
        self.assertEqual(quality["roe"], {})
        self.assertEqual(value["fcf_yield"], {})

    def test_multiple_tickers_independent(self):
        df = pd.DataFrame([
            _fundamentals_row("AAA", roe=0.2),
            _fundamentals_row("BBB", roe=0.1, debt_equity=1.0),
        ])
        quality, _ = compute_quality_value_raw(df)
        self.assertEqual(set(quality["roe"].keys()), {"AAA", "BBB"})
        self.assertEqual(set(quality["debt_equity_inv"].keys()), {"BBB"})


if __name__ == "__main__":
    unittest.main()
