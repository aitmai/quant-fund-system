import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers.price_provider_base import PriceProviderError
from src.providers.sec_edgar_provider import SECEdgarProvider, _pad_cik


def _fake_company_facts(net_income=None, equity=None, liabilities=None, cash=None,
                          op_income=None, depreciation=None, op_cash_flow=None,
                          capex=None, eps=None, shares=None, net_income_tag="NetIncomeLoss",
                          equity_tag="StockholdersEquity", eps_tag="EarningsPerShareDiluted"):
    """Builds a minimal-but-realistic companyfacts JSON shape."""
    us_gaap = {}

    def duration_concept(entries):
        return {"units": {"USD": [{"start": s, "end": e, "val": v} for s, e, v in entries]}}

    def instant_concept(entries):
        return {"units": {"USD": [{"end": e, "val": v} for e, v in entries]}}

    if net_income:
        us_gaap[net_income_tag] = duration_concept(net_income)
    if equity:
        us_gaap[equity_tag] = instant_concept(equity)
    if liabilities:
        us_gaap["Liabilities"] = instant_concept(liabilities)
    if cash:
        us_gaap["CashAndCashEquivalentsAtCarryingValue"] = instant_concept(cash)
    if op_income:
        us_gaap["OperatingIncomeLoss"] = duration_concept(op_income)
    if depreciation:
        us_gaap["DepreciationDepletionAndAmortization"] = duration_concept(depreciation)
    if op_cash_flow:
        us_gaap["NetCashProvidedByUsedInOperatingActivities"] = duration_concept(op_cash_flow)
    if capex:
        us_gaap["PaymentsToAcquirePropertyPlantAndEquipment"] = duration_concept(capex)
    if eps:
        us_gaap[eps_tag] = duration_concept(eps)
    if shares:
        us_gaap["CommonStockSharesOutstanding"] = instant_concept(shares)

    return {"facts": {"us-gaap": us_gaap, "dei": {}}}


class TestSECEdgarProvider(unittest.TestCase):
    def setUp(self):
        self.provider = SECEdgarProvider(user_agent="test test@example.com")

    def test_requires_user_agent(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(PriceProviderError):
                SECEdgarProvider()

    def test_pad_cik_to_ten_digits(self):
        self.assertEqual(_pad_cik("320193"), "0000320193")
        self.assertEqual(_pad_cik("0000320193"), "0000320193")

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_computes_roe_from_net_income_and_equity(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0].roe, 0.1)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_computes_debt_equity_from_liabilities_and_equity(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 5000000)],
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193")
        self.assertAlmostEqual(rows[0].debt_equity, 0.5)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_falls_back_to_profit_loss_tag_when_net_income_loss_absent(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 500000)],
            equity=[("2025-12-31", 5000000)],
            net_income_tag="ProfitLoss",
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193")
        self.assertAlmostEqual(rows[0].roe, 0.1)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_instant_fact_uses_nearest_prior_date_not_exact_match(self, mock_fetch):
        # Equity reported a few days before the income statement's period
        # end — common in practice (different filing/tagging dates).
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-28", 10000000)],
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193")
        self.assertAlmostEqual(rows[0].roe, 0.1)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_earnings_variance_needs_at_least_two_quarters(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            eps=[("2025-10-01", "2025-12-31", 1.5)],
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193")
        self.assertIsNone(rows[0].earnings_variance)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_earnings_variance_computed_with_multiple_quarters(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[
                ("2025-01-01", "2025-03-31", 100),
                ("2025-04-01", "2025-06-30", 100),
                ("2025-07-01", "2025-09-30", 100),
                ("2025-10-01", "2025-12-31", 100),
            ],
            equity=[("2025-12-31", 10000)],
            eps=[
                ("2025-01-01", "2025-03-31", 1.0),
                ("2025-04-01", "2025-06-30", 1.2),
                ("2025-07-01", "2025-09-30", 0.9),
                ("2025-10-01", "2025-12-31", 1.3),
            ],
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193")
        last_row = sorted(rows, key=lambda r: r.report_date)[-1]
        self.assertIsNotNone(last_row.earnings_variance)
        self.assertGreater(last_row.earnings_variance, 0)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_no_conn_means_ev_ebitda_and_fcf_yield_are_none(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2025-12-31", 1000)],
        )
        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=None)
        self.assertIsNone(rows[0].ev_ebitda)
        self.assertIsNone(rows[0].fcf_yield)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_ev_ebitda_computed_when_conn_provides_price(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            cash=[("2025-12-31", 500000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            depreciation=[("2025-10-01", "2025-12-31", 300000)],
            shares=[("2025-12-31", 1000)],
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (100.0,)  # $100/share close price
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)
        # market_cap = 1000 shares * $100 = 100,000
        # EV = 100,000 + 2,000,000 - 500,000 = 1,600,000
        # EBITDA = 1,200,000 + 300,000 = 1,500,000
        expected = 1_600_000 / 1_500_000
        self.assertAlmostEqual(rows[0].ev_ebitda, expected)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_raises_when_no_usable_facts_at_all(self, mock_fetch):
        mock_fetch.return_value = {"facts": {"us-gaap": {}, "dei": {}}}
        with self.assertRaises(PriceProviderError):
            self.provider.fetch_fundamentals("TEST", "0000320193")

    @patch("src.providers.sec_edgar_provider.requests.get")
    def test_403_raises_with_user_agent_guidance(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_get.return_value = mock_resp
        with self.assertRaises(PriceProviderError) as ctx:
            self.provider._fetch_company_facts("0000320193")
        self.assertIn("User-Agent", str(ctx.exception))

    @patch("src.providers.sec_edgar_provider.requests.get")
    def test_404_raises_not_retryable(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp
        with self.assertRaises(PriceProviderError) as ctx:
            self.provider._fetch_company_facts("0000000000")
        self.assertFalse(ctx.exception.retryable)

    @patch("src.providers.sec_edgar_provider.requests.get")
    def test_429_raises_retryable(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp
        with self.assertRaises(PriceProviderError) as ctx:
            self.provider._fetch_company_facts("0000320193")
        self.assertTrue(ctx.exception.retryable)

    @patch("src.providers.sec_edgar_provider.requests.get")
    def test_sends_user_agent_header(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"facts": {"us-gaap": {}, "dei": {}}}
        mock_get.return_value = mock_resp

        self.provider._fetch_company_facts("0000320193")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["headers"]["User-Agent"], "test test@example.com")

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_warns_when_field_missing_for_all_periods(self, mock_fetch, mock_print):
        # No liabilities data at all -> debt_equity always None
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
        )
        self.provider.fetch_fundamentals("TEST", "0000320193")
        warning_calls = [c for c in mock_print.call_args_list if "debt_equity" in str(c) and "WARNING" in str(c)]
        self.assertTrue(warning_calls)


    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_diagnoses_no_shares_outstanding_at_all(self, mock_fetch, mock_print):
        # No shares tag anywhere -> should name that specifically, not
        # leave it ambiguous with a price_history explanation.
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            # no shares=... passed
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (100.0,)
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("no shares-outstanding XBRL value found", printed)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_diagnoses_no_price_history_when_shares_present(self, mock_fetch, mock_print):
        # Shares ARE present, but price_history has nothing -> should name
        # THAT specifically, not blame missing XBRL data.
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2025-12-31", 1000)],
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = None  # no price_history row at all
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("price_history has no matching rows", printed)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_diagnoses_db_error_distinctly_and_logs_the_exception(self, mock_fetch, mock_print):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2025-12-31", 1000)],
        )
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("connection is closed")

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("database error", printed)
        self.assertIn("connection is closed", printed)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_no_warning_when_at_least_some_periods_succeed(self, mock_fetch, mock_print):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2025-12-31", 1000)],
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.return_value = (100.0,)
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn("market cap never computed", printed)


if __name__ == "__main__":
    unittest.main()
