#!/usr/bin/env bash
# Set up the fine-tunes repo: virtualenv, dependencies, and a .env scaffold.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"

if [ ! -d venv ]; then
  echo "→ Creating virtualenv (venv/) ..."
  "$PY" -m venv venv
else
  echo "→ venv/ already exists, reusing it."
fi

echo "→ Installing dependencies ..."
# Use `python -m pip` (not ./venv/bin/pip): the pip console script hardcodes an
# absolute shebang that breaks if the repo folder is ever renamed/moved.
# Force public PyPI: some machines default pip to a private, auth-gated index.
./venv/bin/python -m pip install -q --upgrade pip
./venv/bin/python -m pip install -q --index-url https://pypi.org/simple/ -r requirements.txt pytest

if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ Created .env from .env.example."
else
  echo "→ .env already exists, leaving it untouched."
fi

echo
echo "✓ Dependencies installed."
echo "Next: add your API keys to .env (see /setup output), then run scripts/start_dev.sh"
