#!/usr/bin/env bash
# config14/15/37/40/48 × portfolio(3-ticker / 2-ticker) × rebal vs baseline
#
# Period: each portfolio starts at the latest listing date among its tickers,
#         ends at the latest available DB date (2026 data included).
#
# Usage:
#   bash rebal/run_batch_rebal_compare.sh
#   REBAL_THRESHOLD=0.15 CAPITAL=300000 bash rebal/run_batch_rebal_compare.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="${PYTHON:-python3}"
CAPITAL="${CAPITAL:-300000}"
REBAL_THRESHOLD="${REBAL_THRESHOLD:-0.15}"
REBAL_COOLDOWN="${REBAL_COOLDOWN:-20}"
OUT_DIR="${OUT_DIR:-${ROOT_DIR}/results/rebal_compare}"
DB_PATH="${DB_PATH:-${ROOT_DIR}/var/data/usaetf_ohlcv_day.db}"

CONFIGS=("config14" "config15" "config37" "config40" "config48")
PORTFOLIOS=(
    "3:LEV3SOX,LEV3GOLD,LEV3NASDAQ"
    "2:LEV3GOLD,LEV3NASDAQ"
)

get_period_for_tickers() {
    "$PYTHON" -c "
import sqlite3, sys
from pathlib import Path

db = Path('${DB_PATH}')
tickers = sys.argv[1:]
conn = sqlite3.connect(db)
starts, ends = [], []
for t in tickers:
    row = conn.execute(
        'SELECT MIN(date), MAX(date) FROM usaetf_ohlcv_day WHERE ticker=?', (t,)
    ).fetchone()
    if not row[0]:
        raise SystemExit(f'no data for {t}')
    starts.append(row[0])
    ends.append(row[1])
conn.close()
fmt = lambda d: f'{d[:4]}-{d[4:6]}-{d[6:8]}'
print(f'{fmt(max(starts))}:{fmt(max(ends))}')
" "$@"
}

mkdir -p "$OUT_DIR"
SUMMARY="${OUT_DIR}/summary_latest_listing.json"
echo "[" > "$SUMMARY"
FIRST=1

echo "================================================================"
echo " bbdbt-rebal batch compare (latest-listing start, through 2026)"
echo " capital=$CAPITAL  threshold=${REBAL_THRESHOLD}  cooldown=$REBAL_COOLDOWN"
echo " output=$OUT_DIR"
echo "================================================================"
printf "%-10s %-6s %-8s %-22s %-8s %+10s %+8s %+10s %+8s %+6s\n" \
    "config" "port" "mode" "period" "thresh%" "total_pnl%" "CAGR%" "final_eq" "MDD%" "rebal#"
echo "----------------------------------------------------------------"

for cfg in "${CONFIGS[@]}"; do
    cfg_path="${ROOT_DIR}/auto_trader/${cfg}.json"
    if [[ ! -f "$cfg_path" ]]; then
        echo "[WARN] missing: $cfg_path" >&2
        continue
    fi

    for port_spec in "${PORTFOLIOS[@]}"; do
        n_tickers="${port_spec%%:*}"
        tickers_csv="${port_spec#*:}"
        IFS=',' read -ra TICKERS <<< "$tickers_csv"
        port_label="${n_tickers}t"
        PERIOD=$(get_period_for_tickers "${TICKERS[@]}")

        for mode in baseline rebal; do
            extra_args=()
            if [[ "$mode" == "baseline" ]]; then
                extra_args+=(--no-rebal)
            else
                extra_args+=(--rebal-threshold "$REBAL_THRESHOLD" --rebal-cooldown "$REBAL_COOLDOWN")
            fi

            json_out=$(
                "$PYTHON" "${SCRIPT_DIR}/backtest_rebal.py" \
                    --period "$PERIOD" \
                    --capital "$CAPITAL" \
                    --config "$cfg_path" \
                    --tickers "${TICKERS[@]}" \
                    --json \
                    "${extra_args[@]}" \
                    2>/dev/null
            )

            if [[ -z "$json_out" ]]; then
                printf "%-10s %-6s %-8s %-22s %s\n" "$cfg" "$port_label" "$mode" "$PERIOD" "ERROR"
                continue
            fi

            total_pnl=$(echo "$json_out" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['portfolio']['total_pnl_pct'])")
            cagr=$(echo "$json_out" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['portfolio']['cagr_pct'])")
            final_eq=$(echo "$json_out" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['portfolio']['final_equity'])")
            mdd=$(echo "$json_out" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['portfolio']['mdd_pct'])")
            rebal_cnt=$(echo "$json_out" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['n_rebal_events'])")

            thr_label="-"
            if [[ "$mode" == "rebal" ]]; then
                thr_label=$(echo "$REBAL_THRESHOLD * 100" | bc)
            fi

            printf "%-10s %-6s %-8s %-22s %7s%% %+10s%% %+7s%% %10.0f %+7s%% %6s\n" \
                "$cfg" "$port_label" "$mode" "$PERIOD" "$thr_label" "$total_pnl" "$cagr" "$final_eq" "$mdd" "$rebal_cnt"

            if [[ $FIRST -eq 0 ]]; then echo "," >> "$SUMMARY"; fi
            FIRST=0
            echo "$json_out" >> "$SUMMARY"
        done
    done
    echo ""
done

echo "]" >> "$SUMMARY"
echo "================================================================"
echo "Done. JSON summary: $SUMMARY"
