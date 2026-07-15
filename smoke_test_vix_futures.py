from src.providers.vix_futures_provider import fetch_term_structure

snapshot = fetch_term_structure()

print(f"Front month ({snapshot.front_month.contract_expiration}): {snapshot.front_month.settle}")
print(f"Next month ({snapshot.next_month.contract_expiration}): {snapshot.next_month.settle}")
print(f"Signal: {snapshot.term_structure_signal}")
print(f"Roll yield: {snapshot.roll_yield_pct:.2f}%")