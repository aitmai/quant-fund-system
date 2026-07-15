"""
Hedge sleeve contract sizing — Phase 9, DESIGN.md §3 fix #7:
"Sizing in contracts, not raw dollars: contracts = floor(target_dollar_
allocation / (contract_multiplier x premium)) — accepts rounding rather
than assuming fractional/continuous dollar positions."

Pure function, no I/O — options-chain fetching and target-dollar
computation (10% of NAV, per the dual-sleeve split) live elsewhere.
"""

import math
from typing import Optional

STANDARD_EQUITY_OPTION_MULTIPLIER = 100  # SPY puts: 1 contract = 100 shares


def contracts_for_target_allocation(
    target_dollar_allocation: float,
    premium: float,
    contract_multiplier: int = STANDARD_EQUITY_OPTION_MULTIPLIER,
) -> Optional[int]:
    """floor(target_dollar_allocation / (contract_multiplier * premium)).

    Returns None (not 0, not an exception) when premium is missing or
    non-positive — a $0/None premium means the chain quote wasn't
    usable, which is a different situation from a genuine "size to 0
    contracts" decision, and callers (the daily hedge-sizing job) should
    be able to tell the two apart rather than silently logging a 0.

    Returns 0 when the target allocation genuinely doesn't cover even
    one contract at the current premium — that IS a legitimate sizing
    outcome (small target allocation relative to a pricey put), not an
    error.
    """
    if premium is None or premium <= 0:
        return None
    if target_dollar_allocation is None or target_dollar_allocation < 0:
        return None
    cost_per_contract = contract_multiplier * premium
    return math.floor(target_dollar_allocation / cost_per_contract)
