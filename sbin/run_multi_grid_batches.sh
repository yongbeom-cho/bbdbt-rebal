#!/usr/bin/env bash
# Run grid search over predefined ticker × period batches.
# Supports leverage profile switch:
#   --lvg 2x     -> QLD/SSO/USD jobs with configs/grid_2x_lvg.json
#   (default)    -> existing jobs with configs/grid_default.json
# Each job writes under OUT_ROOT/<slug>/ (meta.json, results_all.jsonl, …).
#
# Jobs run sequentially: each run_grid_search.py already uses multiprocessing
# (--workers 0 → max(1, cpu_count-1)) for grid points, so we avoid stacking
# shell-level parallelism on top.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${PYTHON:-python3}"
LVG_PROFILE="default"
OUT_DIR_ARG=""
SKIP_REPORT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lvg)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --lvg (expected: default|2x)" >&2
        exit 1
      fi
      LVG_PROFILE="$2"
      shift 2
      ;;
    --out-dir|--out-root)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for $1" >&2
        exit 1
      fi
      OUT_DIR_ARG="$2"
      shift 2
      ;;
    --no-report)
      SKIP_REPORT=1
      shift
      ;;
    --help|-h)
      cat <<EOF
Usage: $(basename "$0") [--lvg default|2x] [--out-dir PATH] [--no-report]

Defaults:
  --lvg default   Existing job set (SOXL/TQQQ/TECL/LEV3*)
  --lvg 2x        2x leverage job set (QLD/SSO/USD)
  --out-dir PATH  Batch output root (default: var/grid_batches/<UTC stamp>)
  --no-report     Skip batch_report_*.json (use report_grid_batch_summary.py separately)

Environment:
  GRID_WORKERS    parallel workers per job
  OUT_ROOT        same as --out-dir (CLI wins if both set)
EOF
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

case "${LVG_PROFILE}" in
  default)
    DEFAULT_GRID_JSON="${REPO_ROOT}/configs/grid_default.json"
    ;;
  2x)
    DEFAULT_GRID_JSON="${REPO_ROOT}/configs/grid_2x_lvg.json"
    ;;
  *)
    echo "Unsupported --lvg value: ${LVG_PROFILE} (expected: default|2x)" >&2
    exit 1
    ;;
esac

GRID_JSON="${GRID_JSON:-${DEFAULT_GRID_JSON}}"

if [[ ! -f "${GRID_JSON}" ]]; then
  echo "Grid JSON not found: ${GRID_JSON}" >&2
  exit 1
fi

if [[ -n "${OUT_DIR_ARG}" ]]; then
  OUT_ROOT="${OUT_DIR_ARG}"
elif [[ -n "${OUT_ROOT:-}" ]]; then
  OUT_ROOT="${OUT_ROOT}"
else
  STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
  OUT_ROOT="${REPO_ROOT}/var/grid_batches/${STAMP}"
fi
mkdir -p "${OUT_ROOT}"

echo "Grid   : ${GRID_JSON}"
echo "Out    : ${OUT_ROOT}"
echo "Profile: ${LVG_PROFILE}"
echo ""

run_grid() {
  local ticker="$1"
  local period="$2"
  local slug="${ticker}_$(echo "${period}" | tr ':' '_')"
  local job_dir="${OUT_ROOT}/${slug}"
  echo "=== ${ticker} ${period} -> ${job_dir}"
  # Build argv in one array so we never expand an empty "${optional[@]}" under `set -u` (bash 3.2).
  local cmd=(
    "${PYTHON}" "${REPO_ROOT}/sbin/run_grid_search.py" "${ticker}"
    --period "${period}"
    --grid "${GRID_JSON}"
    --out-dir "${job_dir}"
  )
  if [[ -n "${GRID_WORKERS:-}" ]]; then
    cmd+=(--workers "${GRID_WORKERS}")
  fi
  "${cmd[@]}"
}

if [[ "${LVG_PROFILE}" == "2x" ]]; then
  # 2x leverage jobs: ticker|period (9 independent grids), one after another
  JOBS=(
    "USD|2010-01-01:2021-12-31"
    "USD|2022-01-01:2026-04-12"
    "QLD|2010-01-01:2021-12-31"
    "QLD|2022-01-01:2026-04-12"
    "UPRO|2010-01-01:2021-12-31"
    "UPRO|2022-01-01:2026-04-12"
    "LEV2SOXX|1999-01-01:2026-04-12"
    "LEV2QQQ|1999-01-01:2026-04-12"
    "LEV3SPY|1999-01-01:2026-04-12"
  )
else
  # Default jobs: ticker|period (8 independent grids), one after another
  JOBS=(
    "SOXL|2010-01-01:2021-12-31"
    "SOXL|2022-01-01:2026-04-12"
    "TQQQ|2010-01-01:2021-12-31"
    "TQQQ|2022-01-01:2026-04-12"
    "TECL|2010-01-01:2021-12-31"
    "TECL|2022-01-01:2026-04-12"
    "LEV3SOXX|1999-01-01:2026-04-12"
    "LEV3QQQ|1999-01-01:2026-04-12"
  )
fi

for job in "${JOBS[@]}"; do
  IFS='|' read -r ticker period <<< "${job}"
  run_grid "${ticker}" "${period}" || exit 1
done

echo ""
echo "Done. Outputs under: ${OUT_ROOT}"

if [[ "${SKIP_REPORT}" -eq 0 ]]; then
  BATCH_REPORT_JSON="${REPO_ROOT}/results/batch_report_${LVG_PROFILE}.json"
  mkdir -p "${REPO_ROOT}/results"
  echo ""
  echo "Running batch report -> ${BATCH_REPORT_JSON}"
  "${PYTHON}" "${REPO_ROOT}/sbin/report_grid_batch_summary.py" "${OUT_ROOT}" \
    --objective-top 2 \
    --cross-top-k 2 \
    --json-out "${BATCH_REPORT_JSON}"
fi
