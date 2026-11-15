#!/usr/bin/env bash
# ONE-CLICK START (Linux/Mac):  bash RUN.sh   -> installs on first start, then opens the dashboard
cd "$(dirname "$0")"
export WH_OPEN_BROWSER=1
if [ ! -x .venv/bin/python ]; then bash setup.sh || exit 1; fi
.venv/bin/python app.py