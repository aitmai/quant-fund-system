import yfinance as yf
from datetime import date

spy = yf.Ticker("SPY")
today = date.today()

expirations = spy.options
print(f"Total expirations available: {len(expirations)}")

# Find expirations in the 30-45 DTE window
in_window = []
for exp_str in expirations:
    exp_date = date.fromisoformat(exp_str)
    dte = (exp_date - today).days
    if 30 <= dte <= 45:
        in_window.append((exp_str, dte))

print("Expirations in 30-45 DTE window:", in_window)

if not in_window:
    print("Nothing in window today — showing full list with DTE for reference:")
    for exp_str in expirations:
        dte = (date.fromisoformat(exp_str) - today).days
        print(f"  {exp_str}: {dte} DTE")
else:
    exp_str, dte = in_window[0]
    chain = spy.option_chain(exp_str)
    puts = chain.puts
    price = spy.history(period="1d")["Close"].iloc[-1]
    print(f"\nSPY price: {price}")
    print(f"Chain for {exp_str} ({dte} DTE), {len(puts)} puts")
    print("\nStrikes near current price:")
    near_money = puts[(puts["strike"] > price * 0.85) & (puts["strike"] < price * 1.05)]
    print(near_money[["strike", "bid", "ask", "impliedVolatility", "openInterest"]].to_string())