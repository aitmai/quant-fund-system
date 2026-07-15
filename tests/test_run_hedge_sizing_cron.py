import importlib.util
import sys
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

sys.path.insert(0, ".")
spec = importlib.util.spec_from_file_location(
    "run_hedge_sizing_cron", "scripts/run_hedge_sizing_cron.py"
)
run_hedge_sizing_cron = importlib.util.module_from_spec(spec)
sys.modules["run_hedge_sizing_cron"] = run_hedge_sizing_cron
spec.loader.exec_module(run_hedge_sizing_cron)

from src.providers.options_provider import SelectedPut
from src.providers.vix_futures_provider import TermStructureSnapshot, VixFuturesQuote


class TestTargetDollarAllocation(unittest.TestCase):
    @patch.dict("os.environ", {"HEDGE_SLEEVE_NAV_PLACEHOLDER": "2000000"})
    def test_ten_percent_of_placeholder_nav(self):
        self.assertAlmostEqual(run_hedge_sizing_cron.target_dollar_allocation(), 200_000.0)

    @patch.dict("os.environ", {}, clear=True)
    def test_default_placeholder_used_when_unset(self):
        self.assertAlmostEqual(run_hedge_sizing_cron.target_dollar_allocation(), 100_000.0)


class TestRun(unittest.TestCase):
    def _selected_put(self):
        return SelectedPut(
            strike=736.0, days_to_expiration=30, expiration_date="2026-08-14",
            delta=-0.30, premium=6.36, underlying_price=751.83, implied_vol=0.1476,
        )

    def _vix_quote(self, exp=date(2026, 8, 19), settle=18.73):
        return VixFuturesQuote(exp, date(2026, 7, 14), settle=settle, open_interest=50)

    @patch("run_hedge_sizing_cron.garch_forecast_annualized")
    @patch("run_hedge_sizing_cron.realized_volatility_annualized")
    @patch("run_hedge_sizing_cron.fetch_spy_price_series")
    @patch("run_hedge_sizing_cron.fetch_effective_sizing_quote")
    @patch("run_hedge_sizing_cron.fetch_term_structure")
    @patch("run_hedge_sizing_cron.select_spy_hedge_put")
    @patch.dict("os.environ", {"HEDGE_SLEEVE_NAV_PLACEHOLDER": "1000000"})
    def test_writes_expected_row_and_returns_summary(
        self, mock_select_put, mock_term_structure, mock_sizing_quote,
        mock_spy_prices, mock_realized_vol, mock_garch,
    ):
        mock_select_put.return_value = self._selected_put()
        mock_term_structure.return_value = TermStructureSnapshot(
            front_month=self._vix_quote(date(2026, 7, 22), 17.77),
            next_month=self._vix_quote(date(2026, 8, 19), 18.73),
        )
        mock_sizing_quote.return_value = (self._vix_quote(), False)
        mock_spy_prices.return_value = MagicMock()
        mock_realized_vol.return_value = 0.1371
        mock_garch.return_value = 0.1213

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        summary = run_hedge_sizing_cron.run(mock_conn, as_of=date(2026, 7, 14))

        # 10% of 1,000,000 = 100,000 target; floor(100000 / (100 * 6.36)) = 157
        self.assertEqual(summary["contracts_held"], 157)
        self.assertAlmostEqual(summary["target_dollars"], 100_000.0)
        self.assertFalse(summary["vix_rolled"])
        self.assertEqual(summary["term_structure_signal"], "contango")

        mock_cursor.execute.assert_called_once()
        sql, params = mock_cursor.execute.call_args[0]
        self.assertIn("INSERT INTO vol_hedge_state", sql)
        self.assertIn("ON CONFLICT (date) DO UPDATE", sql)
        # implied_vol param must be the put's IV, not delta (regression
        # guard for a real bug caught during development).
        self.assertAlmostEqual(params[4], 0.1476)

    @patch("run_hedge_sizing_cron.garch_forecast_annualized")
    @patch("run_hedge_sizing_cron.realized_volatility_annualized")
    @patch("run_hedge_sizing_cron.fetch_spy_price_series")
    @patch("run_hedge_sizing_cron.fetch_effective_sizing_quote")
    @patch("run_hedge_sizing_cron.fetch_term_structure")
    @patch("run_hedge_sizing_cron.select_spy_hedge_put")
    def test_rolled_flag_produces_rolled_hedge_action(
        self, mock_select_put, mock_term_structure, mock_sizing_quote,
        mock_spy_prices, mock_realized_vol, mock_garch,
    ):
        mock_select_put.return_value = self._selected_put()
        mock_term_structure.return_value = TermStructureSnapshot(
            front_month=self._vix_quote(date(2026, 7, 22), 17.77),
            next_month=self._vix_quote(date(2026, 8, 19), 18.73),
        )
        mock_sizing_quote.return_value = (self._vix_quote(date(2026, 8, 19), 18.73), True)
        mock_spy_prices.return_value = MagicMock()
        mock_realized_vol.return_value = 0.1371
        mock_garch.return_value = 0.1213

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

        summary = run_hedge_sizing_cron.run(mock_conn, as_of=date(2026, 7, 15))

        self.assertTrue(summary["vix_rolled"])
        _, params = mock_cursor.execute.call_args[0]
        self.assertEqual(params[13], "rolled_and_sized")  # hedge_action position


if __name__ == "__main__":
    unittest.main()
