"""
Multi-ticker portfolio backtest with drift-based cash-only rebalancing.

Runs bear-bull drop-buy strategy per ticker; at EOD, if weight drift >= threshold,
transfers cash from overweight → underweight (no extra stock sells).

Usage:
    python backtest_rebal.py --period 2010-01-01:2025-12-31 \\
        --tickers LEV3SOX LEV3GOLD LEV3NASDAQ \\
        --config auto_trader/config40.json \\
        --capital 300000 [--rebal-threshold 0.15] [--no-rebal]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date as date_type
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

_REBAL = Path(__file__).resolve().parent
_ROOT = _REBAL.parent
_SRC = _ROOT / "src"
for _p in (str(_SRC), str(_REBAL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, default_project_db_path, load_period
from bear_bull_drop_buy.drop_buy import Lot, buy_from_cash, mark_lots_value, sell_lot
from bear_bull_drop_buy.metrics import EquityStats, annualized_cagr_trading_days, equity_curve_stats
from bear_bull_drop_buy.params import StrategyParams
from bear_bull_drop_buy.portfolio import drop_buy_sizing_equity, total_equity
from bear_bull_drop_buy.regime import bull_regime_by_day

from rebal_logic import RebalEvent, check_drift, execute_cash_only_rebal


@dataclass
class TickerState:
    lots: List[Lot] = field(default_factory=list)
    cash: float = 0.0


@dataclass
class MultiBacktestResult:
    portfolio_stats: EquityStats
    portfolio_equity: np.ndarray
    ticker_stats: Dict[str, EquityStats]
    ticker_equity: Dict[str, np.ndarray]
    rebal_events: List[RebalEvent]
    final_cash: Dict[str, float]
    final_lots: Dict[str, List[Lot]]
    dates: List[str]
    rebal_enabled: bool = True


def _apply_strategy_bar(
    ts: TickerState,
    *,
    i: int,
    closes: np.ndarray,
    bull_flags: np.ndarray,
    regime_min_i: int,
    params: StrategyParams,
    buy_mult: float,
    sell_mult: float,
) -> None:
    close_px = float(closes[i])
    if i == 0:
        return

    prev_close = float(closes[i - 1])
    if prev_close <= 0:
        return

    day_ret = close_px / prev_close - 1.0
    bull = bool(bull_flags[i]) if i >= regime_min_i else False

    tp_drop = params.bull_take_profit_pct if bull else params.bear_take_profit_pct
    day_drop_pct = params.bull_day_drop_buy_pct if bull else params.bear_day_drop_buy_pct
    eq_buy_frac = params.bull_equity_buy_frac if bull else params.bear_equity_buy_frac
    surge_pct = (
        params.bull_day_surge_partial_exit_pct if bull
        else params.bear_day_surge_partial_exit_pct
    )
    sell_n = max(
        1,
        int(params.bull_day_surge_sell_newest_n if bull else params.bear_day_surge_sell_newest_n),
    )

    lots = ts.lots
    cash = ts.cash

    j = 0
    while j < len(lots):
        if close_px >= lots[j].entry * (1.0 + tp_drop):
            cash, lots = sell_lot(cash, lots, j, close_px, sell_mult)
            continue
        j += 1

    if lots and day_ret >= surge_pct:
        k = min(sell_n, len(lots))
        for _ in range(k):
            cash, lots = sell_lot(cash, lots, len(lots) - 1, close_px, sell_mult)

    sizing_eq = drop_buy_sizing_equity(cash, lots, close_px)
    if day_ret <= -day_drop_pct:
        spend = eq_buy_frac * sizing_eq
        if spend > 0:
            cash, lots, _lot = buy_from_cash(cash, lots, close_px, spend, buy_mult)

    ts.lots = lots
    ts.cash = cash


def run_multi_backtest_rebal(
    ohlcv_by_ticker: Dict[str, pd.DataFrame],
    params: StrategyParams,
    initial_capital: float = 300_000.0,
    rebal_threshold: float = 0.15,
    rebal_cooldown_sessions: int = 20,
    rebal_enabled: bool = True,
) -> MultiBacktestResult:
    params.validate()
    tickers = list(ohlcv_by_ticker.keys())
    n = len(tickers)
    capital_per = initial_capital / n

    buy_mult = 1.0 + params.commission + params.slippage
    sell_mult = 1.0 - params.commission - params.slippage

    common_idx: pd.DatetimeIndex | None = None
    for t in tickers:
        idx = ohlcv_by_ticker[t].index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    common_idx = common_idx.sort_values()  # type: ignore[union-attr]

    closes_by_ticker: Dict[str, np.ndarray] = {}
    bull_flags_by_ticker: Dict[str, np.ndarray] = {}
    regime_min_i_by_ticker: Dict[str, int] = {}
    date_to_idx_by_ticker: Dict[str, Dict[str, int]] = {}

    for t in tickers:
        df = ohlcv_by_ticker[t]
        closes_by_ticker[t] = df["close"].to_numpy(dtype=float)
        bf, rmi = bull_regime_by_day(
            df, params.d_interval, params.period, params.regime_ma_type,
        )
        bull_flags_by_ticker[t] = bf
        regime_min_i_by_ticker[t] = rmi
        d2i: Dict[str, int] = {}
        for idx_i, row_idx in enumerate(df.index):
            d = row_idx.date().isoformat() if hasattr(row_idx, "date") else str(row_idx)[:10]
            d2i[d] = idx_i
        date_to_idx_by_ticker[t] = d2i

    ts: Dict[str, TickerState] = {
        t: TickerState(lots=[], cash=capital_per) for t in tickers
    }

    equity_curves: Dict[str, List[float]] = {t: [] for t in tickers}
    portfolio_equity: List[float] = []
    dates: List[str] = []
    rebal_events: List[RebalEvent] = []
    last_close: Dict[str, float] = {}
    cooldown = 0

    for common_row in common_idx:
        date_str = (
            common_row.date().isoformat() if hasattr(common_row, "date") else str(common_row)[:10]
        )
        dates.append(date_str)
        close_by_ticker: Dict[str, float] = {}

        for t in tickers:
            d2i = date_to_idx_by_ticker[t]
            i = d2i.get(date_str)
            if i is None:
                if equity_curves[t]:
                    equity_curves[t].append(equity_curves[t][-1])
                else:
                    equity_curves[t].append(ts[t].cash)
                continue

            close_px = float(closes_by_ticker[t][i])
            close_by_ticker[t] = close_px
            last_close[t] = close_px

            _apply_strategy_bar(
                ts[t],
                i=i,
                closes=closes_by_ticker[t],
                bull_flags=bull_flags_by_ticker[t],
                regime_min_i=regime_min_i_by_ticker[t],
                params=params,
                buy_mult=buy_mult,
                sell_mult=sell_mult,
            )

            eq = total_equity(ts[t].cash, ts[t].lots, close_px)
            equity_curves[t].append(eq)

        port_eq = sum(
            ts[t].cash + mark_lots_value(ts[t].lots, last_close.get(t, 0.0))
            for t in tickers
        )
        portfolio_equity.append(port_eq)

        if not rebal_enabled or len(close_by_ticker) < n:
            if cooldown > 0:
                cooldown -= 1
            continue

        if cooldown > 0:
            cooldown -= 1
            continue

        equity_now = {
            t: ts[t].cash + mark_lots_value(ts[t].lots, close_by_ticker[t])
            for t in tickers
        }
        should_rebal, _weights, _excess = check_drift(equity_now, tickers, rebal_threshold)
        if not should_rebal:
            continue

        cash_by_ticker = {t: ts[t].cash for t in tickers}
        ev, _eq_after = execute_cash_only_rebal(
            cash_by_ticker=cash_by_ticker,
            equity_by_ticker=equity_now,
            tickers=tickers,
            date=date_str,
        )
        if ev is not None:
            for t in tickers:
                ts[t].cash = cash_by_ticker[t]
            rebal_events.append(ev)
            cooldown = rebal_cooldown_sessions
            portfolio_equity[-1] = sum(
                ts[t].cash + mark_lots_value(ts[t].lots, close_by_ticker[t])
                for t in tickers
            )

    port_arr = np.asarray(portfolio_equity, dtype=float)
    port_stats = equity_curve_stats(port_arr, initial_capital)

    ticker_stats: Dict[str, EquityStats] = {}
    ticker_equity_np: Dict[str, np.ndarray] = {}
    for t in tickers:
        arr = np.asarray(equity_curves[t], dtype=float)
        ticker_equity_np[t] = arr
        ticker_stats[t] = equity_curve_stats(arr, capital_per)

    return MultiBacktestResult(
        portfolio_stats=port_stats,
        portfolio_equity=port_arr,
        ticker_stats=ticker_stats,
        ticker_equity=ticker_equity_np,
        rebal_events=rebal_events,
        final_cash={t: ts[t].cash for t in tickers},
        final_lots={t: ts[t].lots for t in tickers},
        dates=dates,
        rebal_enabled=rebal_enabled,
    )


def _cagr_from_dates(total_pnl: float, d0: str, d1: str) -> float:
    d_start = date_type.fromisoformat(d0[:10])
    d_end = date_type.fromisoformat(d1[:10])
    yrs = (d_end - d_start).days / 365.25
    if yrs <= 0:
        return float("nan")
    return ((1.0 + total_pnl) ** (1.0 / yrs) - 1.0) * 100.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tickers", nargs="+", default=["LEV3SOX", "LEV3GOLD", "LEV3NASDAQ"])
    ap.add_argument("--period", required=True, help="e.g. 2010-01-01:2025-12-31")
    ap.add_argument("--db", default="", help="SQLite DB path")
    ap.add_argument("--config", default=str(_ROOT / "auto_trader" / "config40.json"))
    ap.add_argument("--params-json", default="", help="StrategyParams override JSON")
    ap.add_argument("--capital", type=float, default=300_000.0)
    ap.add_argument("--rebal-threshold", type=float, default=0.15)
    ap.add_argument("--rebal-cooldown", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_BARS)
    ap.add_argument("--no-rebal", action="store_true", help="Disable rebalancing (baseline)")
    ap.add_argument("--show-rebal-events", action="store_true")
    ap.add_argument("--json", action="store_true", help="Output JSON summary")
    args = ap.parse_args()

    db_path = args.db.strip() or default_project_db_path()

    base = StrategyParams().to_dict()
    cfg_path = Path(args.config)
    if cfg_path.is_file():
        with cfg_path.open(encoding="utf-8") as f:
            cfg = json.load(f)
        sp = cfg.get("strategy_params") or {}
        base.update(sp)
    if args.params_json:
        with open(args.params_json, encoding="utf-8") as f:
            base.update(json.load(f))
    params = StrategyParams.from_dict(base)

    ohlcv_by_ticker: Dict[str, pd.DataFrame] = {}
    for t in args.tickers:
        df_full, _df_eval = load_period(db_path, t, args.period, warmup_bars=int(args.warmup))
        if df_full.empty:
            print(f"  [WARNING] {t}: no data", file=sys.stderr)
            continue
        ohlcv_by_ticker[t] = df_full

    if not ohlcv_by_ticker:
        raise SystemExit("No ticker data loaded")

    result = run_multi_backtest_rebal(
        ohlcv_by_ticker=ohlcv_by_ticker,
        params=params,
        initial_capital=args.capital,
        rebal_threshold=args.rebal_threshold,
        rebal_cooldown_sessions=args.rebal_cooldown,
        rebal_enabled=not args.no_rebal,
    )

    d_start, d_end = result.dates[0], result.dates[-1]
    s = result.portfolio_stats
    cagr_port = _cagr_from_dates(s.total_pnl, d_start, d_end)
    cagr_td = annualized_cagr_trading_days(s.total_pnl, len(result.dates))

    summary: dict[str, Any] = {
        "tickers": args.tickers,
        "config": str(cfg_path),
        "period": args.period,
        "capital": args.capital,
        "rebal_enabled": result.rebal_enabled,
        "rebal_threshold": args.rebal_threshold if result.rebal_enabled else None,
        "rebal_cooldown": args.rebal_cooldown if result.rebal_enabled else None,
        "n_rebal_events": len(result.rebal_events),
        "start_date": d_start,
        "end_date": d_end,
        "n_trading_days": len(result.dates),
        "portfolio": {
            "total_pnl_pct": round(s.total_pnl * 100, 2),
            "cagr_pct": round(cagr_port, 2) if cagr_port == cagr_port else None,
            "cagr_trading_days_pct": round(cagr_td * 100, 2) if cagr_td == cagr_td else None,
            "final_equity": round(s.final_equity, 2),
            "mdd_pct": round(s.max_drawdown_pct, 2),
        },
        "per_ticker": {},
    }

    for t in result.ticker_equity:
        ts_stat = result.ticker_stats[t]
        summary["per_ticker"][t] = {
            "total_pnl_pct": round(ts_stat.total_pnl * 100, 2),
            "final_equity": round(ts_stat.final_equity, 2),
            "mdd_pct": round(ts_stat.max_drawdown_pct, 2),
            "final_cash": round(result.final_cash.get(t, 0.0), 2),
            "n_lots": len(result.final_lots.get(t, [])),
        }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return

    mode = "rebal" if result.rebal_enabled else "baseline"
    print(f"\n=== Portfolio ({mode}) ===")
    print(f"  tickers     : {', '.join(args.tickers)}")
    print(f"  config      : {cfg_path.name}")
    print(f"  period      : {d_start} ~ {d_end}")
    print(f"  total_pnl   : {s.total_pnl*100:+.2f}%")
    print(f"  CAGR        : {cagr_port:.2f}%")
    print(f"  final_equity: {s.final_equity:,.0f}")
    print(f"  MDD         : {s.max_drawdown_pct:.2f}%")
    if result.rebal_enabled:
        print(f"  rebal_events: {len(result.rebal_events)}")

    print("\n=== Per ticker ===")
    for t in result.ticker_equity:
        ts_stat = result.ticker_stats[t]
        print(
            f"  [{t}] pnl={ts_stat.total_pnl*100:+.2f}%  "
            f"final={ts_stat.final_equity:,.0f}  mdd={ts_stat.max_drawdown_pct:.2f}%  "
            f"cash={result.final_cash.get(t, 0):,.0f}  lots={len(result.final_lots.get(t, []))}"
        )

    if args.show_rebal_events and result.rebal_events:
        print(f"\n=== Rebal events ({len(result.rebal_events)}) ===")
        for ev in result.rebal_events:
            before = "  ".join(f"{t}={v*100:.1f}%" for t, v in ev.weights_before.items())
            after = "  ".join(f"{t}={v*100:.1f}%" for t, v in ev.weights_after.items())
            tf = "  ".join(f"{t}:{v:+.0f}" for t, v in ev.transfers.items())
            print(f"  {ev.date}  [{before}] -> [{after}]  cash={ev.cash_moved:,.0f}  [{tf}]")


if __name__ == "__main__":
    main()
