import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from src.providers.price_provider_base import PriceProviderError
from src.providers.sec_edgar_provider import SECEdgarProvider, _pad_cik


def _fake_company_facts(net_income=None, equity=None, liabilities=None, cash=None,
                          op_income=None, depreciation=None, op_cash_flow=None,
                          capex=None, eps=None, shares=None, dei_shares=None,
                          net_income_tag="NetIncomeLoss",
                          equity_tag="StockholdersEquity", eps_tag="EarningsPerShareDiluted"):
    """Builds a minimal-but-realistic companyfacts JSON shape.

    `shares` populates us-gaap:CommonStockSharesOutstanding (a real
    balance-sheet fact, often only tagged annually for some companies —
    confirmed: WMT). `dei_shares` populates dei:EntityCommonStockSharesOutstanding
    (a cover-page disclosure, often tagged closer to every quarter) —
    kept as a SEPARATE parameter so tests can simulate a company with
    both, either, or neither present."""
    us_gaap = {}
    dei = {}

    def duration_concept(entries):
        return {"units": {"USD": [{"start": s, "end": e, "val": v} for s, e, v in entries]}}

    def instant_concept(entries):
        return {"units": {"USD": [{"end": e, "val": v} for e, v in entries]}}

    def instant_shares_concept(entries):
        return {"units": {"shares": [{"end": e, "val": v} for e, v in entries]}}

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
        us_gaap["CommonStockSharesOutstanding"] = instant_shares_concept(shares)
    if dei_shares:
        dei["EntityCommonStockSharesOutstanding"] = instant_shares_concept(dei_shares)

    return {"facts": {"us-gaap": us_gaap, "dei": dei}}


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

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_mixed_failure_reasons_across_periods_still_gets_a_diagnosis(self, mock_fetch, mock_print):
        # Reproduces the real bug (2026-07-13, ~30 tickers across every
        # sector): older periods fail market cap (price_history only
        # covers ~3 years, these companies have 15-20 years of XBRL
        # history), while a DIFFERENT period fails on missing liabilities.
        # No single reason hits 100% of periods, so the old single-reason
        # checks stayed completely silent despite the field genuinely
        # failing for every period — only the vague generic warning fired.
        mock_fetch.return_value = _fake_company_facts(
            net_income=[
                ("2005-01-01", "2005-12-31", 1000000),   # old period: no price data this far back
                ("2025-10-01", "2025-12-31", 1000000),   # recent period: has price, but no liabilities
            ],
            equity=[("2005-12-31", 10000000), ("2025-12-31", 10000000)],
            op_income=[("2005-01-01", "2005-12-31", 1200000), ("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2005-12-31", 1000), ("2025-12-31", 1000)],
            # liabilities deliberately omitted entirely -> every period
            # that DOES get a market cap will still fail on "no_liabilities"
        )
        conn = MagicMock()
        cursor = MagicMock()
        # Only a recent price row — nothing from 2005, so the old period
        # fails market cap while the recent one succeeds market cap but
        # fails liabilities.
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        self.assertTrue(all(r.ev_ebitda is None for r in rows))  # confirms the field really did fail for every period

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        # The fix: SOME message with an actual breakdown must appear —
        # not silence beyond the generic "was None for ALL" line.
        self.assertIn("no single reason dominates", printed)
        self.assertIn("Breakdown:", printed)

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
    def test_operating_income_found_despite_slightly_different_end_date_than_net_income(self, mock_fetch):
        # Reproduces the real bug (confirmed 2026-07-13, ~30 tickers across
        # every sector): OperatingIncomeLoss and NetIncomeLoss don't always
        # share the exact same `end` date within the same filing (XBRL
        # dimensional/context quirks) — exact-match .get() found nothing
        # even when the data existed a day or two off, while equity/
        # liabilities (already using _nearest()) kept working fine. This
        # is why debt_equity worked but ev_ebitda/fcf_yield failed
        # universally with no company-specific pattern.
        facts = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            shares=[("2025-12-31", 1000)],
        )
        # op_income tagged a couple days off from net_income's end date
        facts["facts"]["us-gaap"]["OperatingIncomeLoss"] = {
            "units": {"USD": [{"start": "2025-10-02", "end": "2025-12-30", "val": 1200000}]}
        }
        mock_fetch.return_value = facts
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        self.assertIsNotNone(rows[0].ev_ebitda)

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
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]  # $100/share close price
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


    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_shares_after_report_date_still_enables_market_cap(self, mock_fetch):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2026-01-20", 1000)],  # ~20 days AFTER the report date
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)
        # market_cap = 1000 * 100 = 100,000; ebitda = 1,200,000
        # ev_ebitda would be None only if market cap failed — assert it didn't
        self.assertIsNotNone(rows[0].ev_ebitda)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_shares_lookup_respects_tolerance_window(self, mock_fetch):
        # A shares date WAY outside the tolerance window (e.g. from a
        # completely different fiscal year) should NOT be used as a stand-in.
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2023-01-01", 1000)],  # ~3 years away — too far
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)
        self.assertIsNone(rows[0].ev_ebitda)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_sparse_us_gaap_shares_does_not_block_richer_dei_source(self, mock_fetch):
        # CONFIRMED (2026-07-14, live WMT data): us-gaap:CommonStockSharesOutstanding
        # had 6 data points total (annual 10-Ks only), while
        # dei:EntityCommonStockSharesOutstanding had 69 (near-quarterly).
        # The OLD "first non-empty source wins" logic committed to the
        # sparse us-gaap source just because it wasn't empty, and never
        # even looked at dei — silently discarding far better coverage.
        # This period's report date only has a dei value, not a us-gaap
        # one (simulating a quarter with no annual filing) — market cap
        # should still resolve via the merged dict.
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2024-01-31", 5000)],       # sparse: only an OLD annual period
            dei_shares=[("2025-12-31", 1000)],   # rich: covers THIS quarter
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)
        # market_cap = 1000 (dei shares for THIS period) * 100 (price) = 100,000
        self.assertIsNotNone(rows[0].ev_ebitda)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_us_gaap_shares_take_precedence_over_dei_on_same_date(self, mock_fetch):
        # When BOTH sources have a value for the exact same date, us-gaap
        # (a real balance-sheet fact) should win over dei (a cover-page
        # disclosure) — confirms the merge order, not just "a merge happens".
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2025-12-31", 1000)],      # us-gaap: should win
            dei_shares=[("2025-12-31", 99999)],  # dei: should be overridden
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)
        # market_cap should be 1000 * 100 = 100,000 (us-gaap shares), not
        # 99999 * 100 — ebitda = op_income(1200000) since no depreciation,
        # enterprise_value = 100,000 + 2,000,000 - 0 = 2,100,000
        expected_ev_ebitda = 2_100_000 / 1_200_000
        self.assertAlmostEqual(rows[0].ev_ebitda, expected_ev_ebitda, places=6)


        # Combined `Liabilities` tag absent entirely (confirmed: WMT) —
        # should sum LiabilitiesCurrent + LiabilitiesNoncurrent instead.
        facts = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
        )
        facts["facts"]["us-gaap"]["LiabilitiesCurrent"] = {
            "units": {"USD": [{"end": "2025-12-31", "val": 3000000}]}
        }
        facts["facts"]["us-gaap"]["LiabilitiesNoncurrent"] = {
            "units": {"USD": [{"end": "2025-12-31", "val": 2000000}]}
        }
        mock_fetch.return_value = facts

        rows = self.provider.fetch_fundamentals("WMT", "0000104169")
        self.assertAlmostEqual(rows[0].debt_equity, 5_000_000 / 10_000_000)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_price_history_fetched_only_once_regardless_of_period_count(self, mock_fetch):
        # Prior implementation queried price_history once PER XBRL period
        # (60-100+ round trips for long filing histories) — this locks in
        # that it's now exactly one query per ticker regardless of how
        # many periods are being processed.
        net_income = [(f"{y}-01-01", f"{y}-12-31", 100) for y in range(2000, 2020)]  # 20 periods
        mock_fetch.return_value = _fake_company_facts(
            net_income=net_income,
            equity=[("2019-12-31", 10000)],
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2019, 1, 1), 50.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        self.assertEqual(cursor.fetchall.call_count, 1)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_ev_ebitda_falls_back_to_net_income_when_operating_income_missing(self, mock_fetch, mock_print):
        # market cap succeeds (shares + price both present), and
        # OperatingIncomeLoss is missing entirely (confirmed real case:
        # WDAY, ICE, PODD) — but NetIncomeLoss IS present, so the
        # NetIncome+Interest+Tax+D&A fallback should now compute ev_ebitda
        # successfully instead of giving up.
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            shares=[("2025-12-31", 1000)],
            # no op_income=... passed
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("WDAY", "0000320193", conn=conn)

        # market_cap = 1000 * 100 = 100,000; EBITDA fallback = net_income
        # (no interest/tax/depreciation supplied) = 1,000,000
        # EV = 100,000 + 2,000,000 - 0 = 2,100,000
        expected = 2_100_000 / 1_000_000
        self.assertAlmostEqual(rows[0].ev_ebitda, expected)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn("ev_ebitda never computed", printed)
        self.assertNotIn("market cap never computed", printed)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_diagnoses_missing_operating_income_when_fallback_also_fails(self, mock_fetch, mock_print):
        # Neither OperatingIncomeLoss NOR NetIncomeLoss/ProfitLoss present
        # -> the fallback has nothing to build on either, so this should
        # still surface as a clear diagnosis, not a silent None.
        mock_fetch.return_value = _fake_company_facts(
            equity=[("2025-12-31", 10000000)],
            liabilities=[("2025-12-31", 2000000)],
            shares=[("2025-12-31", 1000)],
            eps=[("2025-10-01", "2025-12-31", 1.0)],  # gives fetch_fundamentals a report-date anchor
            # no net_income, no op_income
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("neither OperatingIncomeLoss NOR", printed)
        self.assertNotIn("market cap never computed", printed)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_diagnoses_missing_liabilities_when_market_cap_and_op_income_are_fine(self, mock_fetch, mock_print):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            shares=[("2025-12-31", 1000)],
            # no liabilities=... passed
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("no Liabilities value", printed)

    @patch("src.providers.sec_edgar_provider.print")
    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_diagnoses_missing_operating_cash_flow_when_market_cap_is_fine(self, mock_fetch, mock_print):
        mock_fetch.return_value = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            shares=[("2025-12-31", 1000)],
            # no op_cash_flow=... passed
        )
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertIn("no NetCashProvidedByUsedInOperatingActivities", printed)

    @patch.object(SECEdgarProvider, "_fetch_company_facts")
    def test_weighted_average_shares_used_as_last_resort(self, mock_fetch):
        facts = _fake_company_facts(
            net_income=[("2025-10-01", "2025-12-31", 1000000)],
            equity=[("2025-12-31", 10000000)],
            op_income=[("2025-10-01", "2025-12-31", 1200000)],
            liabilities=[("2025-12-31", 2000000)],
            # no shares=... passed under any of the normal tags
        )
        facts["facts"]["us-gaap"]["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "units": {"shares": [{"start": "2025-10-01", "end": "2025-12-31", "val": 500}]}
        }
        mock_fetch.return_value = facts
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        rows = self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)
        # market_cap = 500 shares * $100 = 50,000 -> ev_ebitda should now compute
        self.assertIsNotNone(rows[0].ev_ebitda)

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
        cursor.fetchall.return_value = []  # no price_history rows at all
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
        cursor.fetchall.return_value = [(date(2025, 12, 20), 100.0)]
        conn.cursor.return_value.__enter__.return_value = cursor

        self.provider.fetch_fundamentals("TEST", "0000320193", conn=conn)

        printed = " ".join(str(c) for c in mock_print.call_args_list)
        self.assertNotIn("market cap never computed", printed)


if __name__ == "__main__":
    unittest.main()
