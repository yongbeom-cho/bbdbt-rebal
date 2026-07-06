#!/usr/bin/env python3
"""Run a single backtest from merged params (grid point or defaults + overrides JSON)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bear_bull_drop_buy.backtest import run_backtest
from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, default_project_db_path, load_period
from bear_bull_drop_buy.metrics import buy_and_hold_stats, objective_pnl_times_one_minus_mdd_pow
from bear_bull_drop_buy.params import StrategyParams


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ticker")
    ap.add_argument("--period", required=True)
    ap.add_argument("--db", default="", help="SQLite path (default: <repo>/var/data/usaetf_ohlcv_day.db)")
    ap.add_argument("--params-json", default="", help="Partial StrategyParams overrides JSON file")
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_BARS)
    ap.add_argument("--capital", type=float, default=100_000.0)
    ap.add_argument("--objective-power", type=float, default=1.0)
    ap.add_argument("--debug-trace", action="store_true")
    ap.add_argument(
        "--trade-log",
        action="store_true",
        help="Include chronological fills in output under key 'trades' (date, kind, price, shares, ...).",
    )
    args = ap.parse_args()

    db_path = args.db.strip() or default_project_db_path()
    base = StrategyParams().to_dict()
    if args.params_json:
        with open(args.params_json, encoding="utf-8") as f:
            base.update(json.load(f))
    p = StrategyParams.from_dict(base)

    _, df_eval = load_period(db_path, args.ticker, args.period, warmup_bars=int(args.warmup))
    if df_eval.empty:
        raise SystemExit("empty eval df")

    res = run_backtest(
        df_eval,
        p,
        initial_capital=float(args.capital),
        debug_trace=bool(args.debug_trace),
        trade_log=bool(args.trade_log),
    )
    s = res.stats
    bh = buy_and_hold_stats(df_eval["close"].to_numpy(dtype=float), initial_capital=float(args.capital))
    score = objective_pnl_times_one_minus_mdd_pow(s.total_pnl, s.mdd, power=float(args.objective_power))

    summary = {
        "total_pnl": s.total_pnl,
        "final_equity": s.final_equity,
        "mdd": s.mdd,
        "max_drawdown_pct": s.max_drawdown_pct,
        "buy_hold_total_pnl": bh.total_pnl,
        "objective_score": score,
        "n_lots": len(res.lots_final),
        "cash": res.cash,
    }
    if args.trade_log:
        summary["trades"] = res.trade_events or []

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
