from dotenv import load_dotenv
load_dotenv()

from src.db import get_connection
from src.hedge.spy_data import fetch_spy_price_series
from src.hedge.volatility import daily_returns_pct, garch_forecast_annualized, realized_volatility_annualized

conn = get_connection()
prices = fetch_spy_price_series(conn)
returns = daily_returns_pct(prices)

print(f"{len(prices)} prices, {len(returns)} returns")
print(f"Realized vol (21d, annualized): {realized_volatility_annualized(returns):.4f}")
print(f"GARCH(1,1) forecast (annualized): {garch_forecast_annualized(returns):.4f}")

conn.close()