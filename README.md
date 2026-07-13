# quant-fund-system — aitmai

Phase 0 build scaffold for the systematic $1,000,000 fund design in
[`DESIGN.md`](./DESIGN.md). See [`SETUP.md`](./SETUP.md) to get the Phase 0
milestone (GitHub Actions → Supabase, one row written to `job_runs`) running
end to end.

> ⚠️ Personal quant research and engineering project. Nothing here is
> financial or investment advice. See DESIGN.md §8.6 for the required
> backtest → paper-trade → small-pilot validation path before any real
> capital is deployed.

## What's in this scaffold

| Path | Purpose |
|---|---|
| `migrations/001_initial_schema.sql` | Every table from DESIGN.md §6, run once against Supabase |
| `migrations/002_phase1_ingestion.sql` | Phase 1 additions: provider selection, per-ticker provider audit, daily usage ledger |
| `scripts/test_connection.py` | Phase 0 milestone script — proves DB connectivity |
| `scripts/run_universe_sync.py` | Phase 1: index constituent diffing (S&P 500 / Russell 1000) → `universe` |
| `scripts/upload_manual_tickers.py` | Phase 1: manual ticker list upload → `universe` |
| `scripts/run_ingestion_cron.py` | Phase 1: daily price + fundamentals backfill/maintenance |
| `src/providers/` | Price providers (Tiingo, yfinance) behind a shared interface + automatic fallback; FMP fundamentals client |
| `src/universe/` | Index constituent sources + diffing/upload/liquidity-filter logic |
| `src/ingestion/` | Backfill-then-maintenance orchestration + daily/rolling-30-day budget tracking |
| `.github/workflows/hello_world.yml` | Runs the Phase 0 milestone on GitHub Actions |
| `.github/workflows/keepalive.yml` | Monthly commit preventing GitHub's 60-day scheduled-workflow disable (fix #1) |
| `.github/workflows/ingestion_cron.yml` | Daily price/fundamentals ingestion cron |
| `.github/workflows/universe_sync.yml` | Monthly universe reconciliation cron |
| `requirements.txt` | Pinned exact versions (fix #6 — reproducibility across a multi-week backtest) |
| `.env.example` | Every environment variable this system needs |
| `SETUP.md` | Step-by-step Phase 0 + Phase 1 instructions |
| `DESIGN.md` | The full design spec this scaffold implements |

## Price data providers

Both Tiingo and yfinance are built into the backend behind a shared
`PriceProvider` interface (`src/providers/`). `ingestion_config.active_provider`
in Postgres picks which is tried first; if it fails with a retryable error
(rate limit, timeout, empty/throttled response), ingestion automatically
falls back to the other one for that ticker and records which provider
actually served the data in `ingestion_state.last_provider_used`. A future
GUI toggle (Phase 8) only needs to write to `active_provider` — no other
code changes required.

Default: `yfinance` (no API key needed). Switch to Tiingo by setting
`TIINGO_API_KEY` and running:
```sql
UPDATE ingestion_config SET active_provider = 'tiingo' WHERE data_type = 'price';
```

## Build sequence

This scaffold covers **Phase 0 and Phase 1** (DESIGN.md §13). Phases 2–11 —
the rest of the 6-stage pipeline, exit rules, trade tracking, the GUI, the
hedge sleeve, and the backtest engine — are built incrementally on top of
this, each with its own milestone before moving to the next.

## Security notes (this repo is public)

- Every credential lives in GitHub Secrets — never in code, workflow YAML, or
  anything committed. Workflow run logs are public.
- No fetched market data is ever written into this repository. `.gitignore`
  blocks common data-cache paths defensively, but the real guarantee is
  architectural: no script here writes fetched data anywhere under this
  directory. All of it goes to Supabase.
- Strategy parameters (factor weights, thresholds, exit rules) are visible in
  this repo. That's a deliberate, accepted trade-off for zero-cost compute —
  see DESIGN.md §12.

## License

MIT © 2026 aitmai
