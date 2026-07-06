#!/usr/bin/env bash
# Run bbdbt-rebal live auto_trader from repo root context.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "${ROOT}/auto_trader"
CONFIG="${CONFIG:-config.json}"
ARGS=(--config "${CONFIG}" "$@")
if [[ -f "${ROOT}/.venv/bin/python" ]]; then
  exec "${ROOT}/.venv/bin/python" auto_trade.py "${ARGS[@]}"
fi
if [[ -f "${ROOT}/.venv/Scripts/python.exe" ]]; then
  exec "${ROOT}/.venv/Scripts/python.exe" auto_trade.py "${ARGS[@]}"
fi
exec python3 auto_trade.py "${ARGS[@]}"
