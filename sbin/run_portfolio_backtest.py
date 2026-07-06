#!/usr/bin/env python3
"""Portfolio backtest: runs each ticker independently with equal capital, then combines equity curves."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bear_bull_drop_buy.backtest import run_backtest
from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, default_project_db_path, load_period
from bear_bull_drop_buy.metrics import annualized_cagr_trading_days, equity_curve_stats
from bear_bull_drop_buy.params import StrategyParams


def _bar_iso(idx) -> str:
    if hasattr(idx, "date"):
        return idx.date().isoformat()
    return str(pd.Timestamp(idx).date())


def run_single(db_path: str, ticker: str, period: str, params: StrategyParams,
               capital: float, warmup: int) -> pd.Series:
    """Returns equity curve as a Series indexed by ISO date strings (viewer-style).

    Runs backtest on df_full (warmup + eval), extracts eval-period equity,
    then rebases so first bar == capital — matching how the viewer computes stats.
    """
    from bear_bull_drop_buy.data_loader import parse_period_arg
    eval_start, eval_end = parse_period_arg(period)

    df_full, df_eval = load_period(db_path, ticker, period, warmup_bars=warmup)
    if df_eval.empty:
        raise SystemExit(f"No data for {ticker} in period {period}")

    res = run_backtest(df_full, params, initial_capital=capital)

    # Extract eval-period equity from the full curve
    eval_equity_raw: list[float] = []
    eval_dates: list[str] = []
    for i, idx in enumerate(df_full.index):
        if idx < eval_start or idx > eval_end:
            continue
        eval_equity_raw.append(float(res.equity_curve[i]))
        eval_dates.append(_bar_iso(idx))

    # Rebase so first bar == capital (same as _rebase_equity_for_eval_window in service.py)
    if eval_equity_raw:
        base = eval_equity_raw[0]
        if base > 0:
            factor = capital / base
            eval_equity_raw = [v * factor for v in eval_equity_raw]

    return pd.Series(eval_equity_raw, index=pd.DatetimeIndex(eval_dates), name=ticker)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tickers", nargs="+", help="Ticker symbols")
    ap.add_argument("--period", required=True, help="start:end e.g. 2004-11-18:2026-06-27")
    ap.add_argument("--db", default="", help="SQLite path")
    ap.add_argument("--params-json", default="", help="StrategyParams overrides JSON")
    ap.add_argument("--capital-per-ticker", type=float, default=1.0,
                    help="Initial capital per ticker (default 1.0)")
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_BARS)
    args = ap.parse_args()

    db_path = args.db.strip() or default_project_db_path()
    base = StrategyParams().to_dict()
    if args.params_json:
        with open(args.params_json, encoding="utf-8") as f:
            raw = json.load(f)
        # Support both flat params JSON and auto_trader config.json (nested strategy_params)
        overrides = raw.get("strategy_params", raw)
        base.update(overrides)
    params = StrategyParams.from_dict(base)

    # Run each ticker
    curves: dict[str, pd.Series] = {}
    for t in args.tickers:
        print(f"  Running {t}...", file=sys.stderr)
        curves[t] = run_single(db_path, t, args.period, params,
                               args.capital_per_ticker, args.warmup)

    # Align on common dates (inner join — US ETFs share the same trading calendar)
    df = pd.DataFrame(curves)
    df = df.dropna()  # keep only dates all tickers have data

    total_initial = args.capital_per_ticker * len(args.tickers)
    portfolio = df.sum(axis=1)

    arr = portfolio.to_numpy(dtype=float)
    n_trading_days = len(arr)
    stats = equity_curve_stats(arr, total_initial)
    cagr = annualized_cagr_trading_days(stats.total_pnl, n_trading_days)

    result = {
        "tickers": args.tickers,
        "period": args.period,
        "capital_per_ticker": args.capital_per_ticker,
        "total_initial_capital": total_initial,
        "n_trading_days": n_trading_days,
        "start_date": str(portfolio.index[0].date()),
        "end_date": str(portfolio.index[-1].date()),
        "portfolio": {
            "final_equity": round(stats.final_equity, 4),
            "total_pnl_pct": round(stats.total_pnl * 100, 2),
            "mdd_pct": round(stats.max_drawdown_pct, 2),
            "cagr_pct": round(cagr * 100, 2) if not (cagr != cagr) else None,
        },
        "per_ticker": {},
    }

    for t, s in curves.items():
        s_aligned = s.loc[df.index]
        t_init = args.capital_per_ticker
        t_stats = equity_curve_stats(s_aligned.to_numpy(dtype=float), t_init)
        t_cagr = annualized_cagr_trading_days(t_stats.total_pnl, n_trading_days)
        result["per_ticker"][t] = {
            "final_equity": round(t_stats.final_equity, 4),
            "total_pnl_pct": round(t_stats.total_pnl * 100, 2),
            "mdd_pct": round(t_stats.max_drawdown_pct, 2),
            "cagr_pct": round(t_cagr * 100, 2) if not (t_cagr != t_cagr) else None,
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
