#!/usr/bin/env bash
# Start API (8765) + Vite frontend (5174) for bear-bull drop-buy viewer.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VIEWER="$ROOT/viewer"
API_DIR="$VIEWER/api"
FRONT="$VIEWER/frontend"

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

if ! python3 -c "import fastapi" 2>/dev/null; then
  echo "Installing viewer API deps..."
  python3 -m pip install -q -r "$VIEWER/requirements-viewer.txt"
fi

if [[ ! -d "$FRONT/node_modules" ]]; then
  echo "Installing frontend deps..."
  (cd "$FRONT" && npm install)
fi

cleanup() {
  [[ -n "${API_PID:-}" ]] && kill "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "API: http://127.0.0.1:8765  (docs: /docs)"
(cd "$API_DIR" && python3 -m uvicorn main:app --reload --host 127.0.0.1 --port 8765) &
API_PID=$!

sleep 1
echo "UI:  http://127.0.0.1:5174"
(cd "$FRONT" && npm run dev)
