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
| `migrations/003_sec_edgar_fundamentals.sql` | `universe.cik` column (for EDGAR lookups) + updated fundamentals budget config for the EDGAR provider swap |
| `scripts/test_connection.py` | Phase 0 milestone script — proves DB connectivity |
| `scripts/run_universe_sync.py` | Phase 1: index constituent diffing (S&P 500 / Russell 1000) → `universe` |
| `scripts/upload_manual_tickers.py` | Phase 1: manual ticker list upload → `universe` |
| `scripts/run_ingestion_cron.py` | Phase 1: daily price + fundamentals backfill/maintenance |
| `scripts/run_factor_scoring_cron.py` | Phase 2: weekly factor scoring → `factor_scores` |
| `src/providers/` | Price providers (Tiingo, yfinance) behind a shared interface + automatic fallback; SEC EDGAR fundamentals client |
| `src/universe/` | Index constituent sources + diffing/upload/liquidity-filter logic |
| `src/ingestion/` | Backfill-then-maintenance orchestration + daily/rolling-30-day budget tracking |
| `src/scoring/` | Phase 2: bulk data fetch, momentum/low-vol/quality/value raw factors, sector-relative z-scoring |
| `.github/workflows/hello_world.yml` | Runs the Phase 0 milestone on GitHub Actions |
| `.github/workflows/keepalive.yml` | Monthly commit preventing GitHub's 60-day scheduled-workflow disable (fix #1) |
| `.github/workflows/price_ingestion_cron.yml` | Daily price ingestion cron (scheduled — validated, provider fallback + request pacing in place) |
| `.github/workflows/fundamentals_ingestion_cron.yml` | Fundamentals ingestion cron (manual-trigger-only until the SEC EDGAR provider is validated live) |
| `.github/workflows/universe_sync.yml` | Monthly universe reconciliation cron |
| `.github/workflows/factor_scoring_cron.yml` | Weekly factor scoring cron (manual-trigger-only until validated live) |
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

## Fundamentals data provider

Fundamentals ingestion (`src/providers/sec_edgar_provider.py`) uses **SEC
EDGAR's free XBRL API** — no key, no plan tiers, ever. This replaced FMP
(2026-07-13) after FMP's free tier turned out to plan-gate `ratios`,
`key-metrics`, *and* `income-statement` for essentially every S&P 500
ticker, confirmed via live 402 responses.

The tradeoff: EDGAR hands you raw filed numbers (net income, total
liabilities, shares outstanding, etc.), not pre-computed ratios — ROE,
debt/equity, EV/EBITDA, and FCF yield are all computed from those raw
XBRL facts in code, with the same defensive multi-candidate-tag handling
used elsewhere in this codebase for exactly the reason XBRL tagging is
inconsistent company-to-company. Needs `universe.cik` (SEC's Central
Index Key), which is captured automatically during S&P 500 universe sync
from Wikipedia's constituents table — no separate lookup step required.

Set `SEC_EDGAR_USER_AGENT` in `.env` (a descriptive string identifying
who's making requests — SEC's fair-access policy, not optional; requests
without one commonly get a 403). No other configuration needed.

## Phase 2 — Factor Scoring

`src/scoring/` computes the four factors from DESIGN.md §3 Stage 2 —
momentum, low-volatility, quality, value — as raw z-scores written to
`factor_scores` (no fixed-weight composite; that's Stage 3's job).

Two design calls made 2026-07-13 that DESIGN.md itself doesn't specify:
- **Z-scoring is sector-relative**, not universe-wide (Quality within
  Tech compared to Tech, not to Utilities).
- **Missing data excludes a ticker from that factor** (NULL), never
  imputes a neutral/zero score — an imputed "average" would tell Stage
  3's model something false about a ticker we genuinely have no data on.

Every run prints a diagnostic: how many active tickers are missing each
factor, against the total active universe — not buried in a warning.

**Known gap, flagged not silently decided:** DESIGN.md specifies Quality
as "ROE, margin stability, debt/equity," but the `fundamentals` table
(built in Phase 1) has no margin-history column. `earnings_variance`
(trailing-4-quarter EPS stdev, already computed during ingestion) is
substituted as the stability component — a genuine proxy, not the same
metric. Also, `factor_scores.decile_rank` is left NULL: DESIGN.md's
fix #5 removed the fixed-weight composite this column was presumably
built around, and no per-factor decile meaning is specified anywhere.

## Build sequence

This scaffold covers **Phase 0, Phase 1, and Phase 2** (DESIGN.md §13). Phases 3–11 —
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
