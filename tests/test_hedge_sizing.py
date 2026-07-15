import unittest

from src.hedge.sizing import contracts_for_target_allocation


class TestContractsForTargetAllocation(unittest.TestCase):
    def test_basic_floor_division(self):
        # $10,000 target / (100 shares/contract * $5.20 premium) = 19.23 -> 19
        contracts = contracts_for_target_allocation(target_dollar_allocation=10_000, premium=5.20)
        self.assertEqual(contracts, 19)

    def test_exact_division_no_rounding_needed(self):
        contracts = contracts_for_target_allocation(target_dollar_allocation=5_000, premium=5.00)
        self.assertEqual(contracts, 10)

    def test_target_smaller_than_one_contract_yields_zero_not_none(self):
        # Legitimate sizing outcome, not an error — should be distinguishable from None.
        contracts = contracts_for_target_allocation(target_dollar_allocation=50, premium=5.00)
        self.assertEqual(contracts, 0)
        self.assertIsNotNone(contracts)

    def test_missing_premium_returns_none_not_zero(self):
        self.assertIsNone(contracts_for_target_allocation(target_dollar_allocation=10_000, premium=None))
        self.assertIsNone(contracts_for_target_allocation(target_dollar_allocation=10_000, premium=0))
        self.assertIsNone(contracts_for_target_allocation(target_dollar_allocation=10_000, premium=-1))

    def test_negative_target_allocation_returns_none(self):
        self.assertIsNone(contracts_for_target_allocation(target_dollar_allocation=-100, premium=5.0))

    def test_custom_contract_multiplier_respected(self):
        contracts = contracts_for_target_allocation(
            target_dollar_allocation=10_000, premium=5.20, contract_multiplier=10,
        )
        self.assertEqual(contracts, 192)  # 10_000 / (10 * 5.20) = 192.3 -> 192


if __name__ == "__main__":
    unittest.main()
