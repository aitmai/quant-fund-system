"""
Price provider factory + fallback logic.

Both providers are always built into the backend (per design decision,
2026-07-13). `ingestion_config.active_provider` picks which one is tried
FIRST — that's the column a future GUI toggle (Phase 8) will write to.
If the active provider fails with a retryable error, we automatically
try the other one before giving up on that ticker for the day. Which
provider actually served the data is recorded per-ticker in
ingestion_state.last_provider_used, so the GUI can show it, not just
control it.

Circuit breaker: once a provider reports a rate-limit error, it's
disabled for the REST OF THIS PROCESS (not persisted — a fresh cron run
tomorrow starts clean). Without this, a bad yfinance stretch cascades
into a fallback call to Tiingo for every single failed ticker — and
Tiingo's free tier (~50 req/hour) gets exhausted in minutes, after which
every further fallback attempt just fails too, burning time and quota
for zero benefit. Once we know a provider is rate-limited, there's
nothing to gain from calling it again until the limit window resets,
which won't happen mid-run.
"""

import sys
from datetime import date
from typing import List, Optional, Tuple

from .price_provider_base import PriceBar, PriceProvider, PriceProviderError
from .tiingo_provider import TiingoProvider
from .yfinance_provider import YFinanceProvider

PROVIDER_CLASSES = {
    "tiingo": TiingoProvider,
    "yfinance": YFinanceProvider,
}

# Module-level, per-process only — intentionally not persisted anywhere.
_rate_limited_providers: set = set()


def reset_circuit_breaker():
    """Clears the rate-limited-provider set. Exists for test isolation and
    for long-lived processes that might want to retry after a cooldown —
    normal cron usage (one short-lived process per run) never needs this."""
    _rate_limited_providers.clear()


def _looks_like_rate_limit(exc: PriceProviderError) -> bool:
    return "rate limit" in str(exc).lower()


def get_active_provider_name(conn) -> str:
    """Read the currently configured price provider from ingestion_config.
    Falls back to 'yfinance' (no key required) if the row is somehow missing,
    so ingestion never hard-fails just because config wasn't seeded."""
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active_provider FROM ingestion_config WHERE data_type = 'price'"
            )
            row = cur.fetchone()
    if row and row[0]:
        return row[0]
    return "yfinance"


def _instantiate(provider_name: str) -> Optional[PriceProvider]:
    cls = PROVIDER_CLASSES.get(provider_name)
    if cls is None:
        return None
    try:
        return cls()
    except PriceProviderError as exc:
        # e.g. Tiingo instantiated without TIINGO_API_KEY set — that's a
        # config problem, not a per-ticker problem, so just disable it
        # for this run rather than crashing the whole ingestion job.
        print(f"WARNING: could not initialize provider '{provider_name}': {exc}", file=sys.stderr)
        return None


def fetch_with_fallback(
    conn, ticker: str, start_date: date, end_date: Optional[date] = None
) -> Tuple[List[PriceBar], str]:
    """Fetch price history for `ticker`, trying the active provider first
    and falling back to the other one on retryable failure.

    Returns (bars, provider_name_used). Raises PriceProviderError only if
    BOTH providers fail (or are circuit-broken).
    """
    active_name = get_active_provider_name(conn)
    fallback_name = next((n for n in PROVIDER_CLASSES if n != active_name), None)

    ordered_names = [n for n in (active_name, fallback_name) if n]
    last_error: Optional[PriceProviderError] = None

    for name in ordered_names:
        if name in _rate_limited_providers:
            print(
                f"INFO: skipping provider '{name}' for {ticker} — "
                f"already rate-limited earlier this run.",
                file=sys.stderr,
            )
            continue

        provider = _instantiate(name)
        if provider is None:
            continue
        try:
            bars = provider.fetch_history(ticker, start_date, end_date)
            return bars, name
        except PriceProviderError as exc:
            last_error = exc
            if _looks_like_rate_limit(exc):
                _rate_limited_providers.add(name)
                print(
                    f"WARNING: provider '{name}' hit a rate limit — disabling it "
                    f"for the rest of this run (won't retry until the next scheduled run).",
                    file=sys.stderr,
                )
            print(
                f"INFO: provider '{name}' failed for {ticker} "
                f"(retryable={exc.retryable}): {exc}. "
                + ("Trying fallback." if name == active_name and fallback_name else ""),
                file=sys.stderr,
            )
            continue

    if last_error:
        raise last_error
    raise PriceProviderError(
        f"No usable price provider configured (tried: {ordered_names})", retryable=False
    )
