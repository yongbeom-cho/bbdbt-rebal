#!/usr/bin/env python3
"""Cartesian grid search over StrategyParams (no Optuna)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, default_project_db_path
from bear_bull_drop_buy.params import StrategyParams
from bear_bull_drop_buy.runner import run_grid


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticker", help="Symbol in usaetf_ohlcv_day")
    ap.add_argument("--period", required=True, help="YYYY-MM-DD:YYYY-MM-DD")
    ap.add_argument("--db", default="", help="SQLite path (default: <repo>/var/data/usaetf_ohlcv_day.db)")
    ap.add_argument(
        "--grid",
        default=str(_ROOT / "configs" / "grid_default.json"),
        help="Flat JSON: each key = StrategyParams field; value = list or {min,max,step}. "
        "commission/slippage: scalar only (not swept). Legacy {defaults, axes} is merged flat.",
    )
    ap.add_argument("--workers", type=int, default=0, help="0 = max(1, cpu-1)")
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_BARS)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--objective-power", type=float, default=1.0)
    ap.add_argument(
        "--out-dir",
        default="",
        help="Output directory (default: var/grid_runs/{ticker}_{period}, ':' in period → '_')",
    )
    args = ap.parse_args()

    db_path = args.db.strip() or default_project_db_path()
    template = StrategyParams().to_dict()
    out_dir = args.out_dir.strip() or None

    out = run_grid(
        ticker=args.ticker,
        period=args.period,
        db_path=db_path,
        grid_path=args.grid,
        params_template=template,
        initial_capital=float(args.capital),
        warmup_bars=int(args.warmup),
        workers=int(args.workers),
        objective_power=float(args.objective_power),
        out_dir=out_dir,
    )
    print(out)


if __name__ == "__main__":
    main()
