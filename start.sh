#!/usr/bin/env bash
# Starts the web app:   bash start.sh      then open http://localhost:8000
cd "$(dirname "$0")"
[ -d .venv ] || { echo "Run setup first:  bash setup.sh"; exit 1; }
. .venv/bin/activate
python app.py
