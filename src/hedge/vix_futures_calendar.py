"""
VIX futures expiration calendar — Phase 9 (Hedge Sleeve), DESIGN.md §3.

CBOE/CFE's free settlement-price CSVs (cdn.cboe.com) are addressed by
exact expiration date, not a month code, so the front-month/next-month
contract's expiration date has to be computed rather than looked up.

Rule (CBOE's published VIX futures expiration convention, confirmed
against a real published contract — see tests): a VIX futures contract
for month M expires on the Wednesday that is 30 days before the third
Friday of month M+1. Verified 2026-07-15 against a real CBOE URL:
VX_2027-01-20.csv is the January 2027 contract, and this formula
reproduces 2027-01-20 exactly.

Pure date math, no I/O — fetching the actual CSV lives in
src/providers/vix_futures_provider.py.
"""

from datetime import date, timedelta
from typing import Tuple


def third_friday(year: int, month: int) -> date:
    """The date of the third Friday of the given month/year."""
    first_of_month = date(year, month, 1)
    days_until_friday = (4 - first_of_month.weekday()) % 7  # Friday = weekday 4
    first_friday = first_of_month + timedelta(days=days_until_friday)
    return first_friday + timedelta(weeks=2)


def vix_futures_expiration(contract_year: int, contract_month: int) -> date:
    """Expiration date for the VIX futures contract nominally labeled
    with `contract_month`/`contract_year` (e.g. contract_month=1 for a
    "January" contract). CBOE's rule references the FOLLOWING month's
    third Friday, then steps back to the nearest Wednesday on or before
    the 30-days-prior target — the 30-day offset doesn't always land
    exactly on a Wednesday itself, so this walks backward to find it,
    matching CBOE's actual published calendar rather than assuming an
    exact 30-day arithmetic hit every time."""
    next_month = contract_month + 1
    next_year = contract_year
    if next_month > 12:
        next_month = 1
        next_year += 1

    reference_friday = third_friday(next_year, next_month)
    target = reference_friday - timedelta(days=30)
    while target.weekday() != 2:  # Wednesday = weekday 2
        target -= timedelta(days=1)
    return target


def _next_contract_month(year: int, month: int) -> Tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def front_and_next_month_contracts(as_of: date) -> Tuple[date, date]:
    """Returns (front_month_expiration, next_month_expiration) — the two
    nearest not-yet-expired VIX futures contracts as of `as_of`. Needed
    both for the roll rule (DESIGN.md: roll 5 trading days before
    front-month expiry) and for computing the term-structure signal
    (contango/backwardation is a front-vs-next-month price comparison).

    Starts scanning from the month before `as_of`'s month, since a
    contract nominally labeled for an earlier month can still expire
    later within the current month (VIX futures expire mid-month, not
    at month-end) — starting one month back guarantees the true nearest
    unexpired contract is never skipped.
    """
    year, month = as_of.year, as_of.month
    # Step back one month to make sure we don't skip a contract whose
    # label is "last month" but whose expiration is still ahead of as_of.
    if month == 1:
        year, month = year - 1, 12
    else:
        month -= 1

    unexpired = []
    while len(unexpired) < 2:
        exp = vix_futures_expiration(year, month)
        if exp >= as_of:
            unexpired.append(exp)
        year, month = _next_contract_month(year, month)

    return unexpired[0], unexpired[1]
