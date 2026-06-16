#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

HOST="${LIDARAI_HOST:-0.0.0.0}"
PORT="${LIDARAI_PORT:-8000}"

python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
