# Project: quant-fund-ai (quant-fund-system)

Personal systematic quant research project — a rules-based long/short equity
fund design with a volatility hedge sleeve, built incrementally per
[`DESIGN.md`](./DESIGN.md). See [`SETUP.md`](./SETUP.md) for environment
setup and [`README.md`](./README.md) for what's built so far.

> ⚠️ Personal quant research and engineering project. Nothing in this repo
> is financial or investment advice. DESIGN.md §8.6 requires a
> backtest → paper-trade → small-pilot validation path before any real
> capital is deployed — never skip stages of that path.

## Tech Stack

- **Language:** Python 3.12 (pinned via `.python-version`); dependency
  versions pinned exactly in `requirements.txt` — see the comment at the
  top of that file for why: multi-week GitHub Actions backtests must not
  silently change behavior mid-run from an unpinned dependency update
- **Web/API:** Flask 3.0.3, gunicorn 22.0.0 (GUI in `src/gui/`)
- **Database:** Postgres via Supabase, accessed directly with
  `psycopg2-binary` (see `src/db.py`) — **not** via the `supabase-py`
  client. That package was intentionally removed 2026-07-15: nothing
  imported it, and its `realtime` sub-dependency pinned `websockets<13`,
  which conflicted with `yfinance`'s `websockets>=13` requirement.
  Do not re-add `supabase-py` unless something actually starts using it.
- **Data/modeling:** numpy, pandas, scikit-learn, scipy, `arch` (GARCH
  modeling for the hedge sleeve — DESIGN.md §3)
- **Price data providers:** yfinance (default, no key) and Tiingo,
  behind a shared `PriceProvider` interface in `src/providers/`, with
  automatic fallback on retryable errors
- **Fundamentals data provider:** SEC EDGAR's free XBRL API
  (`src/providers/sec_edgar_provider.py`) — replaced FMP on 2026-07-13
  after FMP's free tier turned out to plan-gate the endpoints this
  project needs (confirmed via live 402s on every S&P 500 ticker tested).
  `src/providers/fmp_fundamentals_provider.py` still exists but is legacy/
  kept for reference only; don't build new fundamentals features against it.
- **Deployment:** GitHub Actions (all cron jobs — public repo, so
  **never** put secrets in workflow YAML or log output), Render
  (`render.yaml`, for the GUI service)

## Project Structure

```
src/db.py                   - Postgres connection layer (psycopg2, direct SQL)
src/providers/               - Price + fundamentals providers, shared interface + fallback
src/universe/                 - Index constituent sourcing, diffing, liquidity filters
src/ingestion/                 - Backfill-then-maintenance orchestration, budget tracking
src/scoring/                   - Stage 2 factor scoring (momentum/low-vol/quality/value, z-scores)
src/hedge/                     - Volatility/options hedge sleeve (Black-Scholes, VIX futures, sizing)
src/gui/                       - Flask GUI (routes, queries, db)
scripts/run_*_cron.py          - Entrypoints for each pipeline stage's cron job
scripts/backfill_*.py          - One-off backfill/repair scripts
migrations/00N_*.sql           - Sequential, idempotent Postgres migrations
tests/                          - pytest suite (28+ files, one per module)
DESIGN.md                       - Full design spec — the source of truth for intended behavior
```

## The Pipeline (six stages — know which one you're touching)

1. **Universe Construction** (`src/universe/`) — index diffing (S&P 500 /
   Russell 1000) + liquidity filters
2. **Factor Scoring** (`src/scoring/`) — momentum, low-vol, quality, value
   as sector-relative z-scores in `factor_scores`; no fixed-weight
   composite (that's Stage 3)
3. **ML Ranking** — trained ranking model, re-scores daily candidates
4. **Correlation Filter** (Strategy 3B) — correlation + sector-cap filter,
   uses sector data
5. **Risk-Parity Position Sizing**
6. **Portfolio-Level Risk Aggregation** + Exit Rules (long sleeve)

Plus a separate **Hedge Sleeve** (10%, volatility/options) running
alongside the six stages.

Before changing scoring, ranking, or sizing logic, read the relevant
section of `DESIGN.md` first — several stages have documented deliberate
deviations from the original design (e.g. Stage 2's sector-relative
z-scoring and NULL-on-missing-data choices) that look like bugs if you
haven't read the reasoning.

## Setup Commands

```bash
python -m venv .venv        # or "venv" — .gitignore covers both names;
                             # check which one already exists in this repo
                             # before creating a new one
source .venv/Scripts/activate   # Windows Git Bash — adjust path if using "venv"
pip install -r requirements.txt
cp .env.example .env
# Fill in .env — see Environment Variables below. Never commit .env
# or put its values in workflow YAML (this repo is public).
```

Run the Phase 0 connectivity check first on a fresh setup:
```bash
python scripts/test_connection.py
```

## Testing

```bash
python -m pytest -q
```

- One test file per module under `tests/` — keep that mapping when adding code
- Tests should not require live network/API access — mock provider calls
  (yfinance, Tiingo, SEC EDGAR, FMP) the way existing tests do
- Run the full suite before considering any pipeline-stage change complete —
  a change to `src/scoring/` or `src/hedge/` can silently affect several
  cron scripts at once

## Coding Conventions

- **Defensive multi-candidate-tag handling** for anything reading XBRL
  facts from SEC EDGAR — tagging is inconsistent company-to-company;
  follow the existing pattern in `sec_edgar_provider.py` rather than
  assuming one canonical tag name
- **Missing data → NULL, never imputed.** If a ticker is missing a
  factor input, exclude it from that factor. Do not substitute a
  neutral/zero/average value — that tells downstream models something
  false about a ticker we have no real data on
- **Flag known gaps explicitly** (a printed diagnostic, a code comment
  referencing DESIGN.md, a NULL column) rather than silently deciding
  how to fill them — see Stage 2's `earnings_variance`-as-proxy-for-
  margin-stability as the existing pattern for this
- **Idempotent migrations and idempotent seed/backfill scripts** — every
  migration in `migrations/` and every `scripts/backfill_*.py` should be
  safe to re-run
- **Manual + cron coexistence** (DESIGN.md §4) — every pipeline stage's
  cron script should support both a scheduled run and a `--manual
  --triggered-by <name>` invocation; keep this dual interface if you add
  a new stage script
- Separate ingestion budgets per data type (price vs. fundamentals) —
  don't merge these tracking mechanisms
- Retryable failures (rate limits, timeouts, throttled/empty responses)
  are distinct from bad/delisted tickers — don't collapse that distinction
  when adding new failure handling; see `PRICE_REQUEST_DELAY_SECONDS`/
  `PRICE_MAX_CONSECUTIVE_FAILURES` comments in `.env.example` for the
  reasoning on early-stop vs. per-ticker retry

## Environment Variables

Authoritative source: `.env.example`. Copy to `.env` locally; set as
Secrets in GitHub Actions and Render — never in code or YAML.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | Yes | Supabase **session pooler** connection string — NOT "Direct connection" (that's IPv6-only and fails from GitHub Actions and many hosts, confirmed 2026-07-13) |
| `SUPABASE_URL` | Yes | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Service-role key — server-side only, never client-exposed |
| `SEC_EDGAR_USER_AGENT` | Yes | Format: `"YourName/YourApp your-email@example.com"` — SEC fair-access policy; requests without one commonly get a 403 |
| `TIINGO_API_KEY` | No | Only needed if `ingestion_config.active_provider` is switched to `tiingo`, or to enable automatic fallback when yfinance is throttled |
| `FRED_API_KEY` | Yes | Per SETUP.md Phase 0 GitHub secrets list |
| `FMP_API_KEY` | Legacy | Kept for reference only — fundamentals ingestion no longer uses FMP |
| `EODHD_OPTIONS_API_KEY` | Deferred | Only needed once hedge-sleeve options backtesting is turned on |
| `SP500_WIKIPEDIA_URL` | No | Override only if Wikipedia's constituents table structure changes and parsing breaks |
| `IWB_HOLDINGS_CSV_URL` | No | Russell 1000 via iShares IWB holdings CSV — best-effort only; the S&P 500 (IVV) equivalent is blocked by iShares bot detection, so expect this to likely fail too. Manual ticker upload is the practical path beyond S&P 500 right now (manually-added tickers have no CIK unless supplied, so EDGAR fundamentals won't be automatic for them) |
| `PRICE_BACKFILL_YEARS` | No | Default `3` |
| `PRICE_REQUEST_DELAY_SECONDS` | No | Default `0.5` — Yahoo throttles request bursts; raise if seeing frequent throttling-looking failures (`"Expecting value: line 1 column 1"` on valid tickers ≠ a bad/delisted ticker) |
| `PRICE_REQUEST_TIMEOUT_SECONDS` | No | Default `15` — prevents one stalled connection from hanging the whole daily run; a timeout here is treated as retryable and falls back to Tiingo |
| `PRICE_MAX_CONSECUTIVE_FAILURES` | No | Default `15` — N consecutive failures across both providers signals broad throttling, not coincidence; run stops early and retries next scheduled run |
| `FUNDAMENTALS_STALENESS_DAYS` | No | Default `80` |
| `FUNDAMENTALS_REQUEST_DELAY_SECONDS` | No | Default `0.2` — EDGAR's real limit is 10 req/sec SEC-wide (not per-key); default stays comfortably under that |
| `FUNDAMENTALS_MAX_CONSECUTIVE_FAILURES` | No | Default `15` — same early-stop protection as price ingestion |
| `HEDGE_SLEEVE_NAV_PLACEHOLDER` | No | Default `1000000` — stands in for real NAV until Phase 7 (Trade Tracking & Positions) exists; replace with real fund size once available |

**Note:** daily ingestion budgets themselves (price=400/day, fundamentals=
5000/day) are authoritative in the Postgres `ingestion_config` table
(seeded by `migrations/001` and `migrations/003`), **not** controlled by
env vars — the vars above only affect backfill window, staleness, and
request pacing.

## What NOT to do

- **Never** put `DATABASE_URL`, Supabase keys, `SEC_EDGAR_USER_AGENT`, or any
  API key in workflow YAML, code, or committed files — this repo is
  **public**, and GitHub Actions logs on a public repo are public too
- **Never** commit fetched market data or model artifacts — enforced by
  `.gitignore` (`data/`, `cache/`, `*.csv`, `*.parquet`, `models/`, `*.pkl`,
  `*.joblib`) but the real guarantee is architectural per DESIGN.md §8.4:
  no ingestion or backtest code path should write fetched data anywhere
  under the repo directory. Fetched data goes to Supabase Postgres/Storage
- **Never** re-add or import `supabase-py` without checking it's actually
  needed — it was deliberately removed for a real dependency conflict
- **Never** unpin or loosen a version in `requirements.txt` without
  understanding the reproducibility reason at the top of that file
- **Don't** impute missing factor/fundamentals data with a placeholder
  value — NULL and exclude, per the existing convention
- **Don't** deploy pipeline changes straight to the live cron schedule —
  DESIGN.md §8.6 requires backtest → paper-trade → small-pilot before
  anything touches real capital or live schedules; several workflows
  are intentionally manual-trigger-only until validated live (check
  `.github/workflows/` before assuming a cron is safe to enable)
- **Don't** silently change Stage 2/3/4/5 methodology (z-scoring basis,
  imputation, sizing formulas) without flagging the deviation the way
  existing code does — these choices compound downstream
- **Don't** use the Supabase "Direct connection" string for `DATABASE_URL`
  — use the session pooler string instead (see Environment Variables)

## Current Focus / Known Issues

- SEC EDGAR fundamentals ingestion has been producing `None` for some
  computed fields (e.g. `ev_ebitda`, `fcf_yield`) for certain tickers
  across all periods — likely a market-cap join/price-data lookup issue,
  under active debugging
- `factor_scores.decile_rank` is currently left NULL (Stage 3's fixed-weight
  composite that used to populate it was removed per DESIGN.md fix #5)
- Fundamentals ingestion and factor scoring cron workflows are
  manual-trigger-only until validated live — don't flip them to scheduled
  without explicit sign-off
