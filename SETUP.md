# Phase 0 Setup — Get the Chain Working End to End

Goal (DESIGN.md §13, Phase 0): a "hello world" job runs on GitHub Actions and
writes one row to Supabase. Nothing pipeline-specific yet — this just proves
GitHub → Supabase → (later) Render all actually talk to each other.

## 1. Create the Supabase project

1. Go to supabase.com, create a free project.
2. In Project Settings → Database, copy the **connection string** (URI format).
   This is your `DATABASE_URL`.
3. In Project Settings → API, copy the **Project URL** and **service_role key**
   (not the anon key — the service role key is for server-side/backend use only,
   never expose it client-side). These are `SUPABASE_URL` and
   `SUPABASE_SERVICE_ROLE_KEY`.

## 2. Run the initial schema migration

From the Supabase dashboard's SQL Editor, paste and run the full contents of
`migrations/001_initial_schema.sql`. This creates every table from DESIGN.md §6
and seeds `ingestion_config` / `exit_rules_config` with the design's default
values. It's idempotent — safe to re-run if you need to.

## 3. Create the GitHub repo (public — per the design's decision to use free

Actions compute)

1. Push this project to `github.com/aitmai/<repo-name>`, set to **public**.
2. Go to Settings → Secrets and variables → Actions, and add:
   - `DATABASE_URL`
   - `SUPABASE_URL`
   - `SUPABASE_SERVICE_ROLE_KEY`
   - `FMP_API_KEY`
   - `FRED_API_KEY`
   - `TIINGO_API_KEY` (optional at this stage — see Phase 1 setup below)

   **Never** put these values in code, workflow YAML, or anything committed —
   workflow logs are public on a public repo (DESIGN.md §8.4).

## 4. Run the Phase 0 milestone

Go to the Actions tab → `phase0-hello-world` → **Run workflow**. If it goes
green, check `job_runs` in Supabase's table editor — you should see one new
row with `stage='phase0_scaffolding'`. That's the entire Phase 0 milestone.

You can also run it locally first to debug faster:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in real values, never commit this file
export $(grep -v '^#' .env | xargs)
python scripts/test_connection.py
```

## 5. Confirm the keepalive workflow is scheduled

The `monthly-keepalive` workflow needs no secrets and just needs to exist —
GitHub will run it automatically per its cron schedule. Confirm it shows up
under the Actions tab as a workflow (it won't have run yet until the 1st of
the month, or trigger it manually once to confirm it works at all).

## Phase 1 — Data Ingestion

Goal (DESIGN.md §13): `universe`, `price_history`, `fundamentals` populated
and self-maintaining via the backfill-then-maintenance cron.

### 1. Run the Phase 1 migration

From Supabase's SQL Editor, run `migrations/002_phase1_ingestion.sql` (after
001, if you haven't already). Adds provider selection, per-ticker provider
audit, and the daily usage ledger.

### 2. Add the new secrets

In addition to Phase 0's secrets, add (Settings → Secrets and variables →
Actions):
- `TIINGO_API_KEY` — only strictly required if you switch
  `ingestion_config.active_provider` to `'tiingo'`; the seeded default
  (`'yfinance'`) needs no key, but setting it anyway means automatic fallback
  works if yfinance gets throttled.

(`FMP_API_KEY` is already required from Phase 0 — Phase 1's fundamentals
ingestion is what actually starts using it.)

### 3. Build the initial universe

Locally or via `workflow_dispatch` on `monthly-universe-sync`:
```bash
python scripts/run_universe_sync.py --manual --triggered-by aitmai
```
This fetches current S&P 500 constituents from Wikipedia's "List of S&P 500
companies" table (free, no key) and diffs them into `universe`. This
replaced an earlier iShares-CSV-based approach that turned out to be
blocked by iShares' bot detection — see `src/universe/index_sources.py`
for the full story. `--include-russell1000` still attempts the iShares
IWB holdings CSV, but given the same bot-detection issue was hit on the
near-identical S&P 500 (IVV) endpoint, expect it to likely return nothing
too; manual ticker upload is the practical way to extend past S&P 500
for now. To add your own tickers on top:
```bash
python scripts/upload_manual_tickers.py --file my_tickers.csv --triggered-by aitmai
```
(CSV needs a `ticker` column; `company_name`, `sector`, `exchange` optional.)

### 4. Kick off the backfill

```bash
python scripts/run_ingestion_cron.py --manual --triggered-by aitmai
```
Run this daily (locally or via `workflow_dispatch` on `daily-ingestion`)
until the backfill completes — at the seeded budgets (400 price / 225
fundamentals-equivalent-tickers per day), a ~3,000-ticker universe takes
about 12 days for fundamentals to fully backfill (DESIGN.md §5); price
backfill is faster since it's one call per ticker regardless of history
length. Once `daily-ingestion`'s schedule takes over, this runs itself.

### 5. Confirm the daily-ingestion and monthly-universe-sync workflows are scheduled

Same as the keepalive check in step 5 above — they need no secrets beyond
what's already set, they just need to show up under the Actions tab.
Trigger each manually once via `workflow_dispatch` to confirm they run
clean before relying on the schedule.

### What's next

Once backfill is progressing (it doesn't need to be *complete* — Stage 2
scores whatever data is available), you're on to **Phase 2 — Factor Scoring**
(DESIGN.md §13).
