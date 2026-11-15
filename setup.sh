#!/usr/bin/env bash
# One-time setup for Linux / macOS:   bash setup.sh
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
$PY -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"'
$PY -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
if [ "$(uname)" = "Linux" ]; then python -m playwright install --with-deps chromium; else python -m playwright install chromium; fi
echo
echo "Optional (free rotating IPs per country):  Linux: sudo apt install tor    macOS: brew install tor"
echo "Setup finished. Start the app with:  bash start.sh"
