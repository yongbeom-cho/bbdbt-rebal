#!/usr/bin/env python3
"""
backtest_rebal.py vs live_rebal_engine parity check (bbdbt-rebal).

Usage:
  python sbin/check_live_parity_rebal.py \\
    --config auto_trader/config60.json \\
    --tickers LEV3GOLD LEV3NASDAQ LEV3SOX \\
    --period 1994-05-04:2025-12-31 \\
    --rebal-threshold 0.34
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date as date_type
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_REBAL = _ROOT / "rebal"
_AUTO = _ROOT / "auto_trader"
for _p in (str(_SRC), str(_REBAL), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, default_project_db_path, load_period
from bear_bull_drop_buy.metrics import equity_curve_stats

from drop_buy_live import strategy_params_from_config
from live_rebal_engine import simulate_live_rebal_path
from backtest_rebal import run_multi_backtest_rebal


def _cagr(total_pnl: float, d0: str, d1: str) -> float:
    yrs = (date_type.fromisoformat(d1) - date_type.fromisoformat(d0)).days / 365.25
    if yrs <= 0:
        return float("nan")
    return ((1.0 + total_pnl) ** (1.0 / yrs) - 1.0) * 100.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=_ROOT / "auto_trader" / "config60.json")
    ap.add_argument("--tickers", nargs="+", default=["LEV3GOLD", "LEV3NASDAQ", "LEV3SOX"])
    ap.add_argument("--period", default="1994-05-04:2025-12-31")
    ap.add_argument("--db", default="")
    ap.add_argument("--capital", type=float, default=300_000.0)
    ap.add_argument("--rebal-threshold", type=float, default=0.34)
    ap.add_argument("--rebal-cooldown", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_BARS)
    ap.add_argument("--expected-cagr", type=float, default=39.8)
    ap.add_argument("--expected-mdd", type=float, default=40.9)
    ap.add_argument("--expected-rebal", type=int, default=6)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    with args.config.open(encoding="utf-8") as f:
        cfg = json.load(f)
    params = strategy_params_from_config(dict(cfg.get("strategy_params") or {}))
    params.validate()
    need_len = int(cfg.get("ohlcv_bars", 200))

    print(f"config : {args.config.name}")
    print(f"period : {args.period}  capital : {args.capital:,.0f}")
    print(f"rebal_threshold : {args.rebal_threshold*100:.2f}%  cooldown : {args.rebal_cooldown}")
    print(f"tickers: {args.tickers}\n")

    db = args.db.strip() or default_project_db_path()
    ohlcv: dict = {}
    for t in args.tickers:
        df_full, _ = load_period(db, t, args.period, warmup_bars=int(args.warmup))
        if df_full.empty:
            raise SystemExit(f"no data for {t}")
        ohlcv[t] = df_full
        print(f"  {t}: {df_full.index[0].date()} ~ {df_full.index[-1].date()}  ({len(df_full)} rows)")

    # ① batch backtest (reference)
    print("\n─── ① backtest_rebal.py (batch) ───────────────────────────────")
    bt = run_multi_backtest_rebal(
        ohlcv_by_ticker=ohlcv,
        params=params,
        initial_capital=args.capital,
        rebal_threshold=args.rebal_threshold,
        rebal_cooldown_sessions=args.rebal_cooldown,
        rebal_enabled=True,
    )
    d0, d1 = bt.dates[0], bt.dates[-1]
    bt_cagr = _cagr(bt.portfolio_stats.total_pnl, d0, d1)
    bt_rebal_dates = [ev.date for ev in bt.rebal_events]
    print(f"  CAGR     : {bt_cagr:.2f}%")
    print(f"  MDD      : {bt.portfolio_stats.max_drawdown_pct:.2f}%")
    print(f"  rebal#   : {len(bt_rebal_dates)}  {bt_rebal_dates}")

    # ② live path (state JSON round-trip each day)
    print("\n─── ② live_rebal_engine (daily state load/save) ────────────────")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        state_path = Path(tf.name)
    live = simulate_live_rebal_path(
        ohlcv_by_ticker=ohlcv,
        params=params,
        tickers=args.tickers,
        initial_capital=args.capital,
        rebal_threshold=args.rebal_threshold,
        rebal_cooldown_sessions=args.rebal_cooldown,
        need_len=need_len,
        state_path=state_path,
        meta_dir=None,
        reset_state=True,
        verbose=args.verbose,
    )
    state_path.unlink(missing_ok=True)
    live_cagr = _cagr(live.stats.total_pnl, live.dates[0], live.dates[-1])
    print(f"  CAGR     : {live_cagr:.2f}%")
    print(f"  MDD      : {live.stats.max_drawdown_pct:.2f}%")
    print(f"  rebal#   : {live.n_rebal_events}  {live.rebal_dates}")

    # compare table
    print("\n" + "=" * 62)
    print(f"{'항목':<20} {'Backtest':>14} {'LiveSim':>14} {'Expected':>12}")
    print("-" * 62)
    print(f"{'CAGR (%)':<20} {bt_cagr:>14.2f} {live_cagr:>14.2f} {args.expected_cagr:>12.1f}")
    print(f"{'MDD (%)':<20} {bt.portfolio_stats.max_drawdown_pct:>14.2f} {live.stats.max_drawdown_pct:>14.2f} {args.expected_mdd:>12.1f}")
    print(f"{'rebal count':<20} {len(bt_rebal_dates):>14} {live.n_rebal_events:>14} {args.expected_rebal:>12}")
    print("=" * 62)

    exp_ok = (
        abs(bt_cagr - args.expected_cagr) < 0.15
        and abs(bt.portfolio_stats.max_drawdown_pct - args.expected_mdd) < 0.15
        and len(bt_rebal_dates) == args.expected_rebal
    )
    if exp_ok:
        print(f"✓ Batch backtest matches expected ~{args.expected_cagr:.1f}% / {args.expected_mdd:.1f}% ({args.expected_rebal})")
    else:
        print("✗ Batch backtest differs from expected sweep result")

    if bt_rebal_dates == live.rebal_dates:
        print("✓ Rebal date sequence: backtest == live")
    else:
        print("✗ Rebal date mismatch!")
        print(f"  bt only  : {sorted(set(bt_rebal_dates) - set(live.rebal_dates))}")
        print(f"  live only: {sorted(set(live.rebal_dates) - set(bt_rebal_dates))}")

    min_len = min(len(bt.portfolio_equity), len(live.portfolio_equity))
    bt_arr = bt.portfolio_equity[:min_len]
    live_arr = live.portfolio_equity[:min_len]
    rel_diff = float(np.max(np.abs(bt_arr - live_arr) / np.maximum(np.abs(bt_arr), 1e-9)))
    print(f"\nEquity max relative diff: {rel_diff*100:.6f}%")
    if rel_diff < 1e-6:
        print("✓ Equity curves identical (backtest == live)")
    elif rel_diff < 0.001:
        print(f"△ Near match (rel diff {rel_diff*100:.4f}% < 0.1%)")
    else:
        print(f"✗ Equity curves differ ({rel_diff*100:.4f}%)")
        worst = int(np.argmax(np.abs(bt_arr - live_arr) / np.maximum(np.abs(bt_arr), 1e-9)))
        print(f"  worst date: {bt.dates[worst]}  bt={bt_arr[worst]:,.2f}  live={live_arr[worst]:,.2f}")


if __name__ == "__main__":
    main()
