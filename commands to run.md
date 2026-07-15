
# Phase 2 check factor scoring
python scripts/run_factor_scoring_cron.py --manual --triggered-by aitmai

# Setup python env
python -m venv venv

# Activate
source .venv/Scripts/activate

# GitHub CLI)
winget install --id GitHub.cli

gh auth login



# Rerun Stage 3 (rescore today's candidates
python scripts/run_ml_ranking_cron.py

# Rerun Stage 4 (this is the one that actually uses sector data 
python scripts/run_correlation_filter_cron.py