#!/usr/bin/env bash
# Live-style rebal backtest: shell loop equivalent (Python replays day-by-day internally).
#
# Usage:
#   bash auto_trader/run_live_rebal_backtest.sh
#   CONFIG=auto_trader/config60.json THRESH=0.34 bash auto_trader/run_live_rebal_backtest.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

CONFIG="${CONFIG:-auto_trader/config60.json}"
TICKERS="${TICKERS:-LEV3GOLD LEV3NASDAQ LEV3SOX}"
PERIOD="${PERIOD:-1994-05-04:2025-12-31}"
CAPITAL="${CAPITAL:-300000}"
THRESH="${THRESH:-0.34}"
COOLDOWN="${REBAL_COOLDOWN:-20}"
RUN_NAME="${RUN_NAME:-config60_3t_d34}"

cd "$ROOT"

echo "=== live rebal backtest replay ==="
echo "config=$CONFIG  tickers=$TICKERS  period=$PERIOD"
echo "threshold=${THRESH}  capital=$CAPITAL  cooldown=$COOLDOWN"
echo ""

"$PYTHON" auto_trader/live_rebal_backtest.py replay \
  --config "$CONFIG" \
  --tickers $TICKERS \
  --period "$PERIOD" \
  --capital "$CAPITAL" \
  --rebal-threshold "$THRESH" \
  --rebal-cooldown "$COOLDOWN" \
  --run-name "$RUN_NAME" \
  --reset

echo ""
echo "State : var/live_rebal/${RUN_NAME}/position_state.json"
echo "Meta  : var/live_rebal/${RUN_NAME}/meta/"
