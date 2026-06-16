#!/usr/bin/env bash
# Start the fine-tunes FastAPI dev server (auto-reloads on code changes).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-5001}"

if [ ! -d venv ]; then
  echo "No venv/ found — run scripts/setup.sh (or /setup) first." >&2
  exit 1
fi

# Free the port in case a previous dev server is still running.
pkill -f "uvicorn app:app" 2>/dev/null || true
lsof -ti "tcp:${PORT}" 2>/dev/null | xargs kill -9 2>/dev/null || true

echo "→ Starting dev server on http://127.0.0.1:${PORT} (Ctrl+C to stop) ..."
exec ./venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port "${PORT}" --reload
