#!/usr/bin/env bash
# start_vibecoding.sh
# Sets up the venv correctly and launches Claude Code in this project.
# Run from Git Bash (MINGW64) on Windows.
#
# Usage:
#   ./start_vibecoding.sh
#
# Edit PROJECT_DIR below if you're running this from somewhere else,
# or just place this file in the project root and leave it as ".".

set -e  # stop on first error, so a failed step doesn't silently continue

PROJECT_DIR="."          # change to a full path if running from elsewhere
VENV_DIR="venv"          # the real, correct folder name for this project
PYTHON_VERSION="3.12"    # matches .python-version

echo "== Step 1/7: Clearing bash's cached command paths =="
hash -r

echo "== Step 2/7: Deactivating any currently-active venv =="
if type deactivate >/dev/null 2>&1; then
    deactivate
fi

echo "== Step 3/7: Moving into project directory =="
cd "$PROJECT_DIR"
echo "Now in: $(pwd)"

echo "== Step 4/7: Setting up venv/ =="
if [ -d "$VENV_DIR" ]; then
    echo "venv/ already exists — reusing it."
else
    echo "Creating venv/ ..."
    if command -v py >/dev/null 2>&1; then
        py -"$PYTHON_VERSION" -m venv "$VENV_DIR"
    else
        python -m venv "$VENV_DIR"
    fi
fi

echo "Activating venv/ ..."
source "$VENV_DIR/Scripts/activate"

echo "Python now in use: $(which python)"
python --version

echo "== Step 5/7: Installing dependencies =="
pip install --upgrade pip
pip install -r requirements.txt

echo "== Step 6/7: Checking for .env =="
if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo "Created .env from .env.example — fill in real values before running anything live."
    elif [ -f "_env.example" ]; then
        cp _env.example .env
        echo "Created .env from _env.example — fill in real values before running anything live."
    else
        echo "No .env.example found — make sure required env vars are set some other way."
    fi
else
    echo ".env already exists — leaving it as is."
fi

echo "== Step 7/7: Running test suite as a health check =="
# Don't let a test failure abort the whole script — you may still want to
# launch Claude Code to go fix whatever broke. Just surface the result.
set +e
python -m pytest -q
TEST_EXIT_CODE=$?
set -e

echo ""
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo "Tests passed. Environment looks healthy."
else
    echo "WARNING: tests failed (exit code $TEST_EXIT_CODE) — environment may be broken,"
    echo "or there's a real bug to fix. Launching Claude Code anyway so you can dig in."
fi

echo ""
echo "Launching Claude Code..."
echo ""
claude
