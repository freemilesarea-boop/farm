#!/usr/bin/env bash
# One-time setup (macOS / Linux): Python venv + Playwright Chromium + dashboard deps.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"'
"$PY" -m venv backend/.venv
backend/.venv/bin/pip install --upgrade pip >/dev/null
backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/python -m playwright install chromium

(cd frontend && npm install --no-audit --no-fund)
[ -f frontend/.env.local ] || cp frontend/.env.example frontend/.env.local
echo "Setup complete. Start with: ./scripts/dev.sh"
