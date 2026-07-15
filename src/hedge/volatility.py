"""
Realized volatility + GARCH(1,1) forward-vol forecast — Phase 9 (Hedge
Sleeve), DESIGN.md §3: "Implied vs. realized volatility spread" and
"GARCH(1,1) forward volatility forecast" are two of the three inputs to
the continuous hedge-sizing signal (the third, VIX term structure,
lives in vix_futures_provider.py).

API verified live 2026-07-15 against the real `arch==7.0.0` package on
synthetic data (this library needs no network access, unlike every
other Phase 9 data source, so it was tested directly rather than
assumed): arch_model(..., mean='Zero', vol='Garch', p=1, q=1).fit() then
.forecast(horizon=1, reindex=False).variance.values[-1, 0] gives the
one-step-ahead forecasted variance, in the same units as the input
returns.

Units: DESIGN.md's vol_hedge_state.implied_vol comes from options data
as an ANNUALIZED DECIMAL (e.g. 0.1476 — confirmed live from the SPY
options chain in options_provider.py). realized_vol and garch_forecast
must be in the SAME units to be comparable (that comparison IS the
"implied vs realized spread" signal) — so both functions here return
annualized decimals, not raw daily std or percent.

This is deliberately NOT the same convention as
src/scoring/momentum_lowvol.py's lowvol factor, which stores raw
(un-annualized, negated) daily-return std — that's fine there because
it's only ever used for cross-sectional RANKING among stocks, never
compared against an absolute externally-sourced number like options IV.
"""

from typing import Optional

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 252
MIN_OBSERVATIONS_FOR_GARCH = 100  # arch's own fit reliably needs a real history, not a few dozen points


def daily_returns_pct(prices: pd.Series) -> pd.Series:
    """Simple daily returns as PERCENT (e.g. 1.2, not 0.012) — the scale
    `arch_model` is documented to work best with numerically (very small
    decimal returns can cause optimizer convergence issues). Drops the
    leading NaN from pct_change()."""
    return (prices.pct_change(fill_method=None) * 100.0).dropna()


def realized_volatility_annualized(returns_pct: pd.Series, window: int = 21) -> Optional[float]:
    """Trailing `window`-day realized volatility (std of daily % returns),
    annualized to a decimal (e.g. 0.18) to match options-implied vol's
    units. Default window of 21 trading days (~1 month) mirrors the
    "~21 trading days ≈ 1 month" convention already used in
    momentum_lowvol.py. Returns None if there isn't enough history for
    even one full window — never a fabricated number from a partial one."""
    if len(returns_pct) < window:
        return None
    trailing = returns_pct.tail(window)
    daily_std_pct = trailing.std(ddof=1)
    if pd.isna(daily_std_pct):
        return None
    return float((daily_std_pct / 100.0) * np.sqrt(TRADING_DAYS_PER_YEAR))


def garch_forecast_annualized(returns_pct: pd.Series, horizon: int = 1) -> Optional[float]:
    """Fits a GARCH(1,1) model (zero-mean, normal innovations — the
    standard baseline spec) on `returns_pct` and returns the
    `horizon`-step-ahead forecasted volatility, annualized to a decimal.

    Returns None (never raises, never a fabricated number) when:
    - there isn't enough history to fit reliably (< MIN_OBSERVATIONS_FOR_GARCH)
    - the optimizer fails to converge (res.convergence_flag != 0) —
      confirmed live that this attribute is exactly how the real arch
      library reports fit failure, not an exception
    """
    if len(returns_pct) < MIN_OBSERVATIONS_FOR_GARCH:
        return None

    from arch import arch_model

    try:
        model = arch_model(returns_pct, mean="Zero", vol="Garch", p=1, q=1, dist="normal")
        result = model.fit(disp="off")
    except Exception:
        return None

    if result.convergence_flag != 0:
        return None

    forecast = result.forecast(horizon=horizon, reindex=False)
    variance_pct_sq = forecast.variance.values[-1, horizon - 1]
    if pd.isna(variance_pct_sq) or variance_pct_sq < 0:
        return None

    daily_vol_pct = np.sqrt(variance_pct_sq)
    return float((daily_vol_pct / 100.0) * np.sqrt(TRADING_DAYS_PER_YEAR))
