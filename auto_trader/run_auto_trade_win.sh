#!/usr/bin/env bash
# bbdbt-rebal live auto_trader loop (Git Bash on Windows).
#
# Usage (Git Bash):
#   bash auto_trader/run_auto_trade_win.sh
#   CONFIG=config2.json bash auto_trader/run_auto_trade_win.sh
#
# Prerequisites:
#   - auto_trader/.env  (app_key, app_secret, acc_no, discord_url)
#   - pip install -r auto_trader/requirements-auto-trader.txt
#   - 23:51 KST trigger → auto_trade waits until US close window internally

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUTO="${ROOT}/auto_trader"
CONFIG="${CONFIG:-config.json}"

cd "${AUTO}"

if [[ -f "${ROOT}/.venv/Scripts/python.exe" ]]; then
  PY="${ROOT}/.venv/Scripts/python.exe"
elif [[ -f "${ROOT}/.venv/bin/python" ]]; then
  PY="${ROOT}/.venv/bin/python"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  PY="python3"
fi

echo "bbdbt-rebal auto_trader  root=${ROOT}  config=${CONFIG}  python=${PY}"

while true; do
  time_hm="$(date +%H%M)"
  if [[ "${time_hm}" == "2351" ]]; then
    echo "=== Start USA AUTO TRADE $(date)  config=${CONFIG} ==="
    "${PY}" auto_trade.py --config "${CONFIG}"
  fi
  sleep 45
done
