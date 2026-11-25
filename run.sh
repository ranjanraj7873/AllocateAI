#!/usr/bin/env bash
# One-command startup: creates a venv (if missing), installs dependencies,
# and launches the API + dashboard on http://localhost:8000
set -e

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment…"
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

echo ""
echo "Starting server at http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""

cd backend
PYTHONPATH=. python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
