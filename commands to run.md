
# Phase 2 check factor scoring
python scripts/run_factor_scoring_cron.py --manual --triggered-by aitmai

# Setup python env
python -m venv venv

# Activate
source .venv/Scripts/activate

# GitHub CLI)
winget install --id GitHub.cli

gh auth login
