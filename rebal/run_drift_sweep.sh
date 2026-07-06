#!/usr/bin/env bash
# Drift threshold sweep 10~25% (2.5% step) for config14/15/37/40/48 × 3t/2t
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"
CAPITAL="${CAPITAL:-300000}"
REBAL_COOLDOWN="${REBAL_COOLDOWN:-20}"
DB_PATH="${DB_PATH:-${ROOT_DIR}/var/data/usaetf_ohlcv_day.db}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/results/rebal_compare}"
OUT_JSON="${OUT_JSON:-${OUT_DIR}/drift_sweep_latest_listing.json}"

if [[ -n "${CONFIGS:-}" ]]; then
    # shellcheck disable=SC2206
    CONFIGS=(${CONFIGS})
else
    CONFIGS=("config14" "config15" "config37" "config40" "config48")
fi
if [[ -n "${THRESHOLDS:-}" ]]; then
    # shellcheck disable=SC2206
    THRESHOLDS=(${THRESHOLDS})
else
    THRESHOLDS=("0.10" "0.125" "0.15" "0.175" "0.20" "0.225" "0.25")
fi
if [[ -n "${PORTFOLIOS:-}" ]]; then
    # shellcheck disable=SC2206
    PORTFOLIOS=(${PORTFOLIOS})
else
    PORTFOLIOS=(
        "3t:LEV3SOX,LEV3GOLD,LEV3NASDAQ"
        "2t:LEV3GOLD,LEV3NASDAQ"
    )
fi
get_period_for_tickers() {
    "$PYTHON" -c "
import sqlite3, sys
from pathlib import Path
db = Path('${DB_PATH}')
tickers = sys.argv[1:]
conn = sqlite3.connect(db)
starts, ends = [], []
for t in tickers:
    row = conn.execute('SELECT MIN(date), MAX(date) FROM usaetf_ohlcv_day WHERE ticker=?', (t,)).fetchone()
    starts.append(row[0]); ends.append(row[1])
conn.close()
fmt = lambda d: f'{d[:4]}-{d[4:6]}-{d[6:8]}'
print(f'{fmt(max(starts))}:{fmt(max(ends))}')
" "$@"
}

mkdir -p "$OUT_DIR"
echo "[" > "$OUT_JSON"
FIRST=1

for cfg in "${CONFIGS[@]}"; do
    cfg_path="${ROOT_DIR}/auto_trader/${cfg}.json"
    [[ -f "$cfg_path" ]] || continue

    for port_spec in "${PORTFOLIOS[@]}"; do
        port_label="${port_spec%%:*}"
        tickers_csv="${port_spec#*:}"
        IFS=',' read -ra TICKERS <<< "$tickers_csv"
        if [[ -n "${PERIOD:-}" ]]; then
            :
        else
            PERIOD=$(get_period_for_tickers "${TICKERS[@]}")
        fi

        # baseline
        json_out=$("$PYTHON" "${SCRIPT_DIR}/backtest_rebal.py" \
            --period "$PERIOD" --capital "$CAPITAL" --config "$cfg_path" \
            --tickers "${TICKERS[@]}" --no-rebal --json 2>/dev/null)
        if [[ -n "$json_out" ]]; then
            [[ $FIRST -eq 0 ]] && echo "," >> "$OUT_JSON"
            FIRST=0
            echo "$json_out" | "$PYTHON" -c "
import json,sys
d=json.load(sys.stdin)
d['port_label']='${port_label}'
d['config_id']='${cfg}'
d['drift_pct']=None
d['mode']='baseline'
print(json.dumps(d, ensure_ascii=False))
" >> "$OUT_JSON"
        fi

        for thr in "${THRESHOLDS[@]}"; do
            json_out=$("$PYTHON" "${SCRIPT_DIR}/backtest_rebal.py" \
                --period "$PERIOD" --capital "$CAPITAL" --config "$cfg_path" \
                --tickers "${TICKERS[@]}" \
                --rebal-threshold "$thr" --rebal-cooldown "$REBAL_COOLDOWN" \
                --json 2>/dev/null)
            [[ -z "$json_out" ]] && continue
            [[ $FIRST -eq 0 ]] && echo "," >> "$OUT_JSON"
            FIRST=0
            echo "$json_out" | "$PYTHON" -c "
import json,sys
d=json.load(sys.stdin)
d['port_label']='${port_label}'
d['config_id']='${cfg}'
d['drift_pct']=float('${thr}')*100
d['mode']='rebal'
print(json.dumps(d, ensure_ascii=False))
" >> "$OUT_JSON"
        done
    done
done

echo "]" >> "$OUT_JSON"
echo "Wrote $OUT_JSON"
