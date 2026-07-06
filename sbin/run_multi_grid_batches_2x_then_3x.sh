#!/usr/bin/env bash
# Run 2x leverage grid batch, then 3x (default) leverage grid batch (sequential),
# then write batch_report_lev2.json / batch_report_lev3.json.
#
# Usage: ./sbin/run_multi_grid_batches_2x_then_3x.sh [--out-dir BASE]
#        [--objective-top N] [--cross-top-k K]
#
# Grid outputs (default):
#   results/grid_batches/lev2/
#   results/grid_batches/lev3/
#
# With --out-dir BASE:
#   BASE/lev2/   BASE/lev3/
#
# Environment: GRID_WORKERS, OUT_ROOT (see run_multi_grid_batches.sh)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUNNER="${SCRIPT_DIR}/run_multi_grid_batches.sh"
REPORT="${SCRIPT_DIR}/report_grid_batch_summary.py"
PYTHON="${PYTHON:-python3}"

EXTRA_ARGS=()
BATCH_BASE=""
OBJECTIVE_TOP="${OBJECTIVE_TOP:-2}"
CROSS_TOP_K="${CROSS_TOP_K:-1}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      cat <<EOF
Usage: $(basename "$0") [options]

Runs grid batches in order, then batch reports:
  1. --lvg 2x      -> results/grid_batches/lev2  -> results/batch_report_lev2.json
  2. --lvg default -> results/grid_batches/lev3  -> results/batch_report_lev3.json

Options (also passed to run_multi_grid_batches.sh except --out-dir / --lvg):
  --out-dir BASE      Grid roots: BASE/lev2 and BASE/lev3 (default: results/grid_batches)
  --objective-top N   Report section A (default: 3)
  --cross-top-k K     Report sections B/B2 top-K (default: 5)

Environment:
  GRID_WORKERS        parallel workers per job
  OBJECTIVE_TOP       same as --objective-top
  CROSS_TOP_K         same as --cross-top-k
EOF
      exit 0 ;;
    --out-dir|--out-root)
      if [[ $# -lt 2 ]]; then echo "Missing value for $1" >&2; exit 1; fi
      BATCH_BASE="$2"; shift 2 ;;
    --objective-top)
      if [[ $# -lt 2 ]]; then echo "Missing value for --objective-top" >&2; exit 1; fi
      OBJECTIVE_TOP="$2"; shift 2 ;;
    --cross-top-k)
      if [[ $# -lt 2 ]]; then echo "Missing value for --cross-top-k" >&2; exit 1; fi
      CROSS_TOP_K="$2"; shift 2 ;;
    --lvg)
      echo "error: --lvg is fixed by this script (2x then default/3x); do not pass --lvg" >&2
      exit 1 ;;
    *)
      EXTRA_ARGS+=("$1")
      shift ;;
  esac
done

if [[ ! -f "${RUNNER}" ]]; then
  echo "Missing runner: ${RUNNER}" >&2
  exit 1
fi
if [[ ! -f "${REPORT}" ]]; then
  echo "Missing report script: ${REPORT}" >&2
  exit 1
fi

if [[ -z "${BATCH_BASE}" ]]; then
  BATCH_BASE="${REPO_ROOT}/results/grid_batches"
fi

OUT_2X="${BATCH_BASE}/lev2"
OUT_3X="${BATCH_BASE}/lev3"
REPORT_2X="${REPO_ROOT}/results/batch_report_lev2.json"
REPORT_3X="${REPO_ROOT}/results/batch_report_lev3.json"

run_phase() {
  local lvg_profile="$1"
  local out_dir="$2"
  echo "Grid output: ${out_dir}"
  if ((${#EXTRA_ARGS[@]} > 0)); then
    bash "${RUNNER}" --lvg "${lvg_profile}" --out-dir "${out_dir}" --no-report "${EXTRA_ARGS[@]}"
  else
    bash "${RUNNER}" --lvg "${lvg_profile}" --out-dir "${out_dir}" --no-report
  fi
}

run_report() {
  local grid_root="$1"
  local json_out="$2"
  local label="$3"
  echo ""
  echo "========== Report (${label}): ${json_out} =========="
  if [[ ! -d "${grid_root}" ]]; then
    echo "error: grid root not found: ${grid_root}" >&2
    exit 1
  fi
  mkdir -p "${REPO_ROOT}/results"
  "${PYTHON}" "${REPORT}" "${grid_root}" \
    --objective-top "${OBJECTIVE_TOP}" \
    --cross-top-k "${CROSS_TOP_K}" \
    --json-out "${json_out}"
}



echo ""
echo "========== Phase 2/2: 3x leverage (default profile) =========="
run_phase default "${OUT_3X}"
run_report "${OUT_3X}" "${REPORT_3X}" "lev3"

echo "========== Phase 1/2: 2x leverage =========="
run_phase 2x "${OUT_2X}"
run_report "${OUT_2X}" "${REPORT_2X}" "lev2"

echo ""
echo "All phases complete (2x then 3x + reports)."
echo "  Grid 2x : ${OUT_2X}"
echo "  Grid 3x : ${OUT_3X}"
echo "  Report  : ${REPORT_2X}"
echo "  Report  : ${REPORT_3X}"
