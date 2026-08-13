#!/usr/bin/env bash
# Start the trainer. Serves the built UI and the API on one port.
set -euo pipefail

cd "$(dirname "$0")"
PY="${PY:-/Users/test/miniconda3/bin/python3}"
PORT="${PORT:-8787}"

# Coach commentary defaults to the local model via Ollama. Override with
# COACH_BACKEND=anthropic (plus ANTHROPIC_API_KEY) to use the hosted model.
export COACH_BACKEND="${COACH_BACKEND:-local}"
export COACH_MODEL="${COACH_MODEL:-qwen3:14b}"

# Stockfish is started on demand and released after this many idle seconds.
export ENGINE_IDLE_TIMEOUT="${ENGINE_IDLE_TIMEOUT:-120}"

if [ "$COACH_BACKEND" = "local" ] && ! curl -sf localhost:11434/api/version >/dev/null; then
  echo "warning: ollama not responding on :11434 — 'brew services start ollama'"
fi

if [ ! -d frontend/dist ]; then
  echo "building frontend..."
  (cd frontend && npm install --silent && npm run build)
fi

echo "http://localhost:${PORT}"
cd backend
exec "$PY" -m uvicorn server:app --port "$PORT"
