#!/usr/bin/env python3
"""
Live rebal backtest runner.

Modes:
  replay  — day-by-day loop with state/meta files (simulates shell daily run)
  summary — print stats from saved meta + state

Usage:
  python auto_trader/live_rebal_backtest.py replay \\
    --config auto_trader/config60.json \\
    --tickers LEV3GOLD LEV3NASDAQ LEV3SOX \\
    --period 1994-05-04:2025-12-31 \\
    --rebal-threshold 0.34 --capital 300000 --reset

  bash auto_trader/run_live_rebal_backtest.sh   # wrapper
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date as date_type
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))
if str(_ROOT / "auto_trader") not in sys.path:
    sys.path.insert(0, str(_ROOT / "auto_trader"))

from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, default_project_db_path, load_period
from bear_bull_drop_buy.metrics import annualized_cagr_trading_days

from drop_buy_live import strategy_params_from_config
from live_rebal_engine import simulate_live_rebal_path


def _cagr(total_pnl: float, d0: str, d1: str) -> float:
    yrs = (date_type.fromisoformat(d1) - date_type.fromisoformat(d0)).days / 365.25
    if yrs <= 0:
        return float("nan")
    return ((1.0 + total_pnl) ** (1.0 / yrs) - 1.0) * 100.0


def cmd_replay(args: argparse.Namespace) -> None:
    with Path(args.config).open(encoding="utf-8") as f:
        cfg = json.load(f)
    params = strategy_params_from_config(dict(cfg.get("strategy_params") or {}))
    params.validate()
    need_len = int(cfg.get("ohlcv_bars", 200))

    db = args.db.strip() or default_project_db_path()
    ohlcv = {}
    for t in args.tickers:
        df_full, _ = load_period(db, t, args.period, warmup_bars=int(args.warmup))
        if df_full.empty:
            raise SystemExit(f"no data for {t}")
        ohlcv[t] = df_full

    run_dir = Path(args.run_dir) if args.run_dir else _ROOT / "var" / "live_rebal" / args.run_name
    state_path = run_dir / "position_state.json"
    meta_dir = run_dir / "meta"

    result = simulate_live_rebal_path(
        ohlcv_by_ticker=ohlcv,
        params=params,
        tickers=args.tickers,
        initial_capital=args.capital,
        rebal_threshold=args.rebal_threshold,
        rebal_cooldown_sessions=args.rebal_cooldown,
        need_len=need_len,
        state_path=state_path,
        meta_dir=meta_dir,
        reset_state=args.reset,
        verbose=args.verbose,
    )

    d0, d1 = result.dates[0], result.dates[-1]
    s = result.stats
    cagr = _cagr(s.total_pnl, d0, d1)
    cagr_td = annualized_cagr_trading_days(s.total_pnl, len(result.dates))

    summary = {
        "mode": "live_replay",
        "config": str(args.config),
        "tickers": args.tickers,
        "period": args.period,
        "rebal_threshold_pct": round(args.rebal_threshold * 100, 2),
        "start_date": d0,
        "end_date": d1,
        "n_trading_days": len(result.dates),
        "n_rebal_events": result.n_rebal_events,
        "rebal_dates": result.rebal_dates,
        "portfolio": {
            "total_pnl_pct": round(s.total_pnl * 100, 2),
            "cagr_pct": round(cagr, 2),
            "cagr_trading_days_pct": round(cagr_td * 100, 2),
            "final_equity": round(s.final_equity, 2),
            "mdd_pct": round(s.max_drawdown_pct, 2),
        },
        "state_path": str(state_path),
        "meta_dir": str(meta_dir),
    }
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("replay", help="Full history day-by-day with state files")
    rp.add_argument("--config", default=str(_ROOT / "auto_trader" / "config60.json"))
    rp.add_argument("--tickers", nargs="+", default=["LEV3GOLD", "LEV3NASDAQ", "LEV3SOX"])
    rp.add_argument("--period", default="1994-05-04:2025-12-31")
    rp.add_argument("--db", default="")
    rp.add_argument("--capital", type=float, default=300_000.0)
    rp.add_argument("--rebal-threshold", type=float, default=0.34)
    rp.add_argument("--rebal-cooldown", type=int, default=20)
    rp.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_BARS)
    rp.add_argument("--run-dir", default="")
    rp.add_argument("--run-name", default="config60_3t_d34")
    rp.add_argument("--reset", action="store_true", help="Clear state/meta before run")
    rp.add_argument("--verbose", action="store_true")
    rp.set_defaults(func=cmd_replay)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
