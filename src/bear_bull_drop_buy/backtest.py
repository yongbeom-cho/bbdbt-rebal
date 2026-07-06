"""Bear/bull regime drop-buy backtest."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np
import pandas as pd

from bear_bull_drop_buy.drop_buy import Lot, buy_from_cash, sell_lot
from bear_bull_drop_buy.metrics import EquityStats, equity_curve_stats
from bear_bull_drop_buy.params import StrategyParams
from bear_bull_drop_buy.portfolio import drop_buy_sizing_equity, total_equity
from bear_bull_drop_buy.regime import bull_regime_by_day


@dataclass
class BacktestResult:
    stats: EquityStats
    equity_curve: np.ndarray
    lots_final: List[Lot] = field(default_factory=list)
    cash: float = 0.0
    debug_bars: Optional[List[dict[str, Any]]] = None
    trade_events: Optional[List[dict[str, Any]]] = None


def run_backtest(
    ohlcv: pd.DataFrame,
    params: StrategyParams,
    initial_capital: float = 100_000.0,
    debug_trace: bool = False,
    trade_log: bool = False,
) -> BacktestResult:
    params.validate()
    df = ohlcv.dropna(subset=["close"]).copy()
    if len(df) < 2:
        z = np.array([initial_capital], dtype=float)
        return BacktestResult(
            stats=equity_curve_stats(z, initial_capital),
            equity_curve=z,
            trade_events=[] if trade_log else None,
        )

    buy_mult = 1.0 + params.commission + params.slippage
    sell_mult = 1.0 - params.commission - params.slippage

    bull_flags, regime_min_i = bull_regime_by_day(
        df,
        params.d_interval,
        params.period,
        params.regime_ma_type,
    )
    lots: List[Lot] = []
    cash = float(initial_capital)

    equities: List[float] = []
    debug_bars: Optional[List[dict[str, Any]]] = [] if debug_trace else None
    trade_events: Optional[List[dict[str, Any]]] = [] if trade_log else None
    closes = df["close"].to_numpy(dtype=float)
    opens = (
        df["open"].to_numpy(dtype=float)
        if "open" in df.columns
        else closes.copy()
    )

    def bar_date(i: int) -> str:
        idx_i = df.index[i]
        return idx_i.date().isoformat() if hasattr(idx_i, "date") else str(idx_i)

    def mark_te(px: float) -> float:
        return total_equity(cash, lots, px)

    def log_trade(bar_i: int, kind: str, **fields: Any) -> None:
        if trade_events is None:
            return
        trade_events.append({
            "date": bar_date(bar_i),
            "kind": kind,
            "n_lots": len(lots),
            **fields,
        })

    for i in range(len(df)):
        close_px = float(closes[i])
        equities.append(mark_te(close_px))

        if i == 0:
            if debug_bars is not None:
                idx_i = df.index[i]
                debug_bars.append({
                    "date": idx_i.date().isoformat() if hasattr(idx_i, "date") else str(idx_i),
                    "close": close_px,
                    "equity": float(mark_te(close_px)),
                    "cash": float(cash),
                    "n_lots": len(lots),
                })
            continue

        prev_close = float(closes[i - 1])
        if prev_close <= 0:
            continue

        day_ret = close_px / prev_close - 1.0
        min_i = regime_min_i
        bull = bool(bull_flags[i]) if i >= min_i else False

        tp_drop = params.bull_take_profit_pct if bull else params.bear_take_profit_pct
        day_drop_pct = params.bull_day_drop_buy_pct if bull else params.bear_day_drop_buy_pct
        eq_buy_frac = params.bull_equity_buy_frac if bull else params.bear_equity_buy_frac
        surge_pct = params.bull_day_surge_partial_exit_pct if bull else params.bear_day_surge_partial_exit_pct
        sell_n = max(1, int(params.bull_day_surge_sell_newest_n if bull else params.bear_day_surge_sell_newest_n))

        # 1) Drop sells: TP + surge (bull/bear params from regime)
        j = 0
        while j < len(lots):
            if close_px >= lots[j].entry * (1.0 + tp_drop):
                lot = lots[j]
                cash, lots = sell_lot(cash, lots, j, close_px, sell_mult)
                log_trade(
                    i,
                    "drop_sell_take_profit",
                    price=float(close_px),
                    shares=float(lot.shares),
                    entry=float(lot.entry),
                    proceeds=float(lot.shares * close_px * sell_mult),
                )
                continue
            j += 1

        if lots and day_ret >= surge_pct:
            k = min(sell_n, len(lots))
            for _ in range(k):
                lot = lots[-1]
                cash, lots = sell_lot(
                    cash, lots, len(lots) - 1, close_px, sell_mult
                )
                log_trade(
                    i,
                    "drop_sell_surge",
                    price=float(close_px),
                    shares=float(lot.shares),
                    entry=float(lot.entry),
                    proceeds=float(lot.shares * close_px * sell_mult),
                    day_ret=float(day_ret),
                )

        # 2) Drop down-day buy (eq_buy_frac applies to total equity)
        sizing_eq = drop_buy_sizing_equity(cash, lots, close_px)
        if day_ret <= -day_drop_pct:
            spend = eq_buy_frac * sizing_eq
            if spend > 0:
                cash, lots, lot = buy_from_cash(
                    cash,
                    lots,
                    close_px,
                    spend,
                    buy_mult,
                )
                if lot.shares > 0:
                    log_trade(
                        i,
                        "drop_buy",
                        price=float(close_px),
                        shares=float(lot.shares),
                        entry=float(lot.entry),
                        cost=float(lot.shares * close_px * buy_mult),
                        day_ret=float(day_ret),
                    )

        equities[-1] = mark_te(close_px)

        if debug_bars is not None:
            idx_i = df.index[i]
            debug_bars.append({
                "date": idx_i.date().isoformat() if hasattr(idx_i, "date") else str(idx_i),
                "close": close_px,
                "equity": float(mark_te(close_px)),
                "cash": float(cash),
                "n_lots": len(lots),
            })

    arr = np.asarray(equities, dtype=float)
    stats = equity_curve_stats(arr, initial_capital)
    return BacktestResult(
        stats=stats,
        equity_curve=arr,
        lots_final=lots,
        cash=cash,
        debug_bars=debug_bars,
        trade_events=trade_events,
    )
