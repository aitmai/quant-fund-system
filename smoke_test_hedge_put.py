from src.providers.options_provider import select_spy_hedge_put

result = select_spy_hedge_put()
print(f"Strike: {result.strike}")
print(f"DTE: {result.days_to_expiration}")
print(f"Expiration: {result.expiration_date}")
print(f"Delta: {result.delta:.4f}")
print(f"Premium: {result.premium}")
print(f"Underlying: {result.underlying_price}")