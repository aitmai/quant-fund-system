"""
Budget tracking (DESIGN.md §5 — Separate Budgets Per Data Type).

Two independent constraints, checked before every ticker is processed:
  1. Daily request budget (ingestion_config.daily_budget), reset each
     calendar day via a fresh ingestion_daily_usage row.
  2. Rolling 30-day bandwidth cap (ingestion_config.monthly_bandwidth_cap_mb),
     only set for fundamentals (FMP's 500MB/30-day limit) — NULL for price,
     since neither Tiingo nor yfinance publish a bandwidth cap.
"""

from datetime import date, timedelta
from typing import Optional, Tuple


def get_config(conn, data_type: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT daily_budget, calls_per_ticker, monthly_bandwidth_cap_mb, active_provider
            FROM ingestion_config WHERE data_type = %s
            """,
            (data_type,),
        )
        row = cur.fetchone()
    if not row:
        raise RuntimeError(
            f"No ingestion_config row for data_type='{data_type}'. "
            f"Did migrations run? See migrations/001_initial_schema.sql."
        )
    return {
        "daily_budget": row[0],
        "calls_per_ticker": row[1] or 1,
        "monthly_bandwidth_cap_mb": row[2],
        "active_provider": row[3],
    }


def get_calls_used_today(conn, data_type: str, today: Optional[date] = None) -> int:
    today = today or date.today()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT calls_used FROM ingestion_daily_usage WHERE data_type = %s AND usage_date = %s",
            (data_type, today),
        )
        row = cur.fetchone()
    return row[0] if row else 0


def get_rolling_30day_bandwidth_mb(conn, data_type: str, today: Optional[date] = None) -> float:
    today = today or date.today()
    window_start = today - timedelta(days=30)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(SUM(bandwidth_used_mb), 0) FROM ingestion_daily_usage
            WHERE data_type = %s AND usage_date >= %s AND usage_date <= %s
            """,
            (data_type, window_start, today),
        )
        row = cur.fetchone()
    return float(row[0]) if row else 0.0


def remaining_ticker_budget(conn, data_type: str, today: Optional[date] = None) -> Tuple[int, str]:
    """Returns (num_tickers_remaining_today, reason_if_zero).

    Checks both the daily request budget and the rolling 30-day bandwidth
    cap (if configured) and returns whichever is more restrictive.
    """
    today = today or date.today()
    cfg = get_config(conn, data_type)
    calls_used = get_calls_used_today(conn, data_type, today)
    calls_remaining = max(0, cfg["daily_budget"] - calls_used)
    tickers_remaining = calls_remaining // cfg["calls_per_ticker"]

    if cfg["monthly_bandwidth_cap_mb"] is not None:
        bandwidth_used = get_rolling_30day_bandwidth_mb(conn, data_type, today)
        if bandwidth_used >= cfg["monthly_bandwidth_cap_mb"]:
            return 0, (
                f"30-day bandwidth cap reached "
                f"({bandwidth_used:.1f}MB / {cfg['monthly_bandwidth_cap_mb']}MB)"
            )

    if tickers_remaining <= 0:
        return 0, f"daily request budget exhausted ({calls_used}/{cfg['daily_budget']} calls used)"

    return tickers_remaining, ""


def record_usage(conn, data_type: str, calls: int, bandwidth_mb: float = 0.0, today: Optional[date] = None):
    today = today or date.today()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ingestion_daily_usage (data_type, usage_date, calls_used, bandwidth_used_mb)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (data_type, usage_date) DO UPDATE SET
                    calls_used = ingestion_daily_usage.calls_used + EXCLUDED.calls_used,
                    bandwidth_used_mb = ingestion_daily_usage.bandwidth_used_mb + EXCLUDED.bandwidth_used_mb
                """,
                (data_type, today, calls, bandwidth_mb),
            )
