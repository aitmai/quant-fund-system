from datetime import date
import yfinance as yf
from src.providers.options_provider import _expirations_in_dte_window, _quotes_for_expiration

spy = yf.Ticker("SPY")
as_of = date.today()
print(f"as_of: {as_of}")

expirations = _expirations_in_dte_window(spy, as_of, 30, 45)
print(f"Expirations in window: {expirations}")

underlying_price = float(spy.history(period="1d")["Close"].iloc[-1])
print(f"Underlying price: {underlying_price}")

all_quotes = []
for exp_str in expirations:
    quotes = _quotes_for_expiration(spy, exp_str, as_of)
    print(f"{exp_str}: {len(quotes)} quotes parsed")
    all_quotes.extend(quotes)

print(f"\nTotal quotes: {len(all_quotes)}")

priceable = [q for q in all_quotes if q.mid_premium is not None]
print(f"Quotes with usable premium: {len(priceable)}")

if priceable:
    sample = priceable[len(priceable)//2]
    print(f"\nSample priceable quote: strike={sample.strike}, dte={sample.days_to_expiration}, "
          f"iv={sample.implied_vol}, bid={sample.bid}, ask={sample.ask}, mid={sample.mid_premium}")
else:
    # show a few raw quotes regardless of premium, to see what's actually there
    print("\nFirst 5 raw quotes (any premium status):")
    for q in all_quotes[:5]:
        print(f"  strike={q.strike}, dte={q.days_to_expiration}, iv={q.implied_vol}, bid={q.bid}, ask={q.ask}")

from src.hedge.black_scholes import put_delta
if priceable:
    computable_deltas = 0
    for q in priceable[:50]:
        d = put_delta(underlying_price, q.strike, q.days_to_expiration / 365.0, q.implied_vol)
        if d is not None:
            computable_deltas += 1
    print(f"\nComputable deltas (first 50 priceable): {computable_deltas}/{min(50, len(priceable))}")