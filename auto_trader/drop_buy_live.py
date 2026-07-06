"""
Live trading phases mirroring bear_bull_drop_buy.backtest.run_backtest bar logic.

Order per session day (same as backtest):
  Sell phase: (1) drop per-lot TP
              (2) drop surge partial (newest first)
  Buy phase:  (3) down-day drop buy
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bear_bull_drop_buy.drop_buy import Lot, buy_from_cash, sell_lot
from bear_bull_drop_buy.params import StrategyParams
from bear_bull_drop_buy.portfolio import drop_buy_sizing_equity, total_equity
from bear_bull_drop_buy.regime import bull_regime_by_day


@dataclass
class SellFillPlan:
    shares: float
    entry: float
    reason: str
    lot_meta: dict = field(default_factory=dict)


@dataclass
class BuyFillPlan:
    spend_usd: float
    reason: str
    shares: float
    entry: float


@dataclass
class DropBuySellPhaseResult:
    cash: float
    lots: List[Lot]
    sell_plans: List[SellFillPlan] = field(default_factory=list)


@dataclass
class DropBuyBuyPhaseResult:
    cash: float
    lots: List[Lot]
    buy_plans: List[BuyFillPlan] = field(default_factory=list)


def bull_regime_last_bar(df: pd.DataFrame, params: StrategyParams) -> bool:
    flags, min_i = bull_regime_by_day(
        df,
        int(params.d_interval),
        int(params.period),
        params.regime_ma_type,
    )
    i = len(df) - 1
    if i < min_i:
        return False
    return bool(flags[i])


def strategy_params_from_config(m: dict, *, defaults: Optional[StrategyParams] = None) -> StrategyParams:
    payload = dict(m)
    if defaults is not None:
        base = defaults.to_dict()
        base.update(payload)
        payload = base
    return StrategyParams.from_dict(payload)


def _pop_meta(metas: Optional[List[dict]], idx: int) -> dict:
    if metas is None:
        return {}
    m = dict(metas[idx])
    metas.pop(idx)
    return m


def apply_drop_buy_sell_phase(
    *,
    prev_close: float,
    close_px: float,
    cash: float,
    lots: List[Lot],
    params: StrategyParams,
    bull: bool,
    commission: float,
    slippage: float,
    lot_metas: Optional[List[dict]] = None,
) -> DropBuySellPhaseResult:
    sell_plans: List[SellFillPlan] = []
    sell_mult = 1.0 - commission - slippage
    lots = [Lot(l.shares, l.entry) for l in lots]
    met = list(lot_metas) if lot_metas is not None else None
    if met is not None and len(met) != len(lots):
        raise ValueError("lot_metas 길이는 lots 와 같아야 함")

    if prev_close <= 0 or close_px <= 0:
        return DropBuySellPhaseResult(
            cash=float(cash),
            lots=lots,
            sell_plans=sell_plans,
        )

    day_ret = close_px / prev_close - 1.0
    tp_drop = params.bull_take_profit_pct if bull else params.bear_take_profit_pct
    surge_pct = (
        params.bull_day_surge_partial_exit_pct if bull else params.bear_day_surge_partial_exit_pct
    )
    sell_n = max(
        1,
        int(params.bull_day_surge_sell_newest_n if bull else params.bear_day_surge_sell_newest_n),
    )
    # 1) Drop per-lot take-profit
    j = 0
    while j < len(lots):
        if close_px >= lots[j].entry * (1.0 + tp_drop):
            lot = lots[j]
            sell_plans.append(
                SellFillPlan(
                    shares=float(lot.shares),
                    entry=float(lot.entry),
                    reason="drop_take_profit_lot",
                    lot_meta=_pop_meta(met, j),
                )
            )
            cash, lots = sell_lot(cash, lots, j, close_px, sell_mult)
            continue
        j += 1

    # 2) Drop surge partial exit (newest first)
    if lots and day_ret >= surge_pct:
        k = min(sell_n, len(lots))
        for _ in range(k):
            j_last = len(lots) - 1
            lot = lots[j_last]
            sell_plans.append(
                SellFillPlan(
                    shares=float(lot.shares),
                    entry=float(lot.entry),
                    reason="drop_surge_partial_newest",
                    lot_meta=_pop_meta(met, j_last),
                )
            )
            cash, lots = sell_lot(cash, lots, j_last, close_px, sell_mult)

    return DropBuySellPhaseResult(
        cash=float(cash),
        lots=lots,
        sell_plans=sell_plans,
    )


def apply_drop_buy_buy_phase(
    *,
    prev_close: float,
    close_px: float,
    cash: float,
    lots: List[Lot],
    params: StrategyParams,
    bull: bool,
    commission: float,
    slippage: float,
) -> DropBuyBuyPhaseResult:
    buy_plans: List[BuyFillPlan] = []
    buy_mult = 1.0 + commission + slippage
    lots = [Lot(l.shares, l.entry) for l in lots]

    if prev_close <= 0 or close_px <= 0:
        return DropBuyBuyPhaseResult(
            cash=float(cash),
            lots=lots,
            buy_plans=buy_plans,
        )

    day_ret = close_px / prev_close - 1.0
    day_drop_pct = params.bull_day_drop_buy_pct if bull else params.bear_day_drop_buy_pct
    eq_buy_frac = params.bull_equity_buy_frac if bull else params.bear_equity_buy_frac

    # 3) Drop down-day buy
    sizing_eq = drop_buy_sizing_equity(cash, lots, close_px)
    if day_ret <= -day_drop_pct:
        spend = float(eq_buy_frac) * float(sizing_eq)
        if spend > 0:
            cash_before = cash
            cash, lots, lot = buy_from_cash(
                cash,
                lots,
                close_px,
                spend,
                buy_mult,
            )
            if lot.shares > 0:
                usd = max(0.0, cash_before - cash)
                buy_plans.append(
                    BuyFillPlan(
                        spend_usd=usd,
                        reason="day_drop",
                        shares=float(lot.shares),
                        entry=float(lot.entry),
                    )
                )

    return DropBuyBuyPhaseResult(
        cash=float(cash),
        lots=lots,
        buy_plans=buy_plans,
    )


def apply_drop_buy_day_for_parity(
    *,
    prev_close: float,
    close_px: float,
    cash: float,
    lots: List[Lot],
    params: StrategyParams,
    bull: bool,
    commission: float,
    slippage: float,
) -> tuple[float, List[Lot]]:
    """Single-bar sell→buy (for parity script vs backtest)."""
    sp = apply_drop_buy_sell_phase(
        prev_close=prev_close,
        close_px=close_px,
        cash=cash,
        lots=lots,
        params=params,
        bull=bull,
        commission=commission,
        slippage=slippage,
        lot_metas=None,
    )
    bp = apply_drop_buy_buy_phase(
        prev_close=prev_close,
        close_px=close_px,
        cash=sp.cash,
        lots=sp.lots,
        params=params,
        bull=bull,
        commission=commission,
        slippage=slippage,
    )
    return bp.cash, bp.lots


def equity_for_parity(cash: float, lots: List[Lot], close_px: float) -> float:
    return float(total_equity(cash, lots, close_px))
