from datetime import date, timedelta
from src.providers.yfinance_provider import YFinanceProvider

provider = YFinanceProvider()
bars = provider.fetch_history("AAPL", start_date=date.today() - timedelta(days=10))

print(f"Got {len(bars)} bars")
for b in bars[-3:]:
    print(b)