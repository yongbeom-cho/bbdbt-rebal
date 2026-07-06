"""Equity curve stats (PnL, MDD)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

import numpy as np


@dataclass
class EquityStats:
    total_pnl: float
    final_equity: float
    mdd: float
    max_drawdown_pct: float
    equity: Optional[np.ndarray] = None


def equity_curve_stats(
    equities: List[float] | np.ndarray,
    initial_capital: float = 1.0,
) -> EquityStats:
    eq = np.asarray(equities, dtype=np.float64)
    if eq.size == 0:
        return EquityStats(
            total_pnl=0.0,
            final_equity=initial_capital,
            mdd=0.0,
            max_drawdown_pct=0.0,
            equity=eq,
        )

    peak = np.maximum.accumulate(eq)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratios = np.where(peak > 0, eq / peak, 1.0)
    min_ratio = float(np.min(ratios))
    min_ratio = min(1.0, max(0.0, min_ratio))
    mdd = 1.0 - min_ratio
    mdd = min(1.0, max(0.0, mdd))
    max_drawdown_pct = mdd * 100.0
    final = float(eq[-1])
    total_pnl = final / initial_capital - 1.0

    return EquityStats(
        total_pnl=total_pnl,
        final_equity=final,
        mdd=mdd,
        max_drawdown_pct=max_drawdown_pct,
        equity=eq,
    )


def _drawdown_episodes(eq: np.ndarray) -> List[Tuple[int, int, float]]:
    """Non-overlapping peak→trough episodes as (peak_idx, trough_idx, depth)."""
    n = int(eq.size)
    if n < 2:
        return []
    episodes: List[Tuple[int, int, float]] = []
    i = 0
    while i < n - 1:
        peak_i = i
        peak_v = float(eq[i])
        j = i + 1
        while j < n and float(eq[j]) >= peak_v - 1e-15:
            if float(eq[j]) > peak_v + 1e-15:
                peak_i = j
                peak_v = float(eq[j])
            j += 1
        if j >= n:
            break
        trough_i = j
        trough_v = float(eq[j])
        k = j + 1
        while k < n and float(eq[k]) < peak_v - 1e-15:
            if float(eq[k]) < trough_v - 1e-15:
                trough_i = k
                trough_v = float(eq[k])
            k += 1
        if peak_v > 0:
            depth = 1.0 - trough_v / peak_v
            if depth > 1e-12:
                episodes.append((peak_i, trough_i, depth))
        i = k if k > peak_i + 1 else peak_i + 1
    return episodes


def top_drawdown_period_indices(
    equities: List[float] | np.ndarray,
    k: int = 3,
) -> List[Tuple[int, int]]:
    """Top-k drawdown episodes by depth (peak_idx, trough_idx)."""
    eq = np.asarray(equities, dtype=np.float64)
    ranked = sorted(_drawdown_episodes(eq), key=lambda x: -x[2])
    return [(peak_i, trough_i) for peak_i, trough_i, _ in ranked[: max(0, int(k))]]


def max_drawdown_period_indices(
    equities: List[float] | np.ndarray,
) -> Optional[Tuple[int, int]]:
    """Bar indices (peak, trough) for the deepest drawdown episode."""
    tops = top_drawdown_period_indices(equities, k=1)
    return tops[0] if tops else None


def buy_and_hold_stats(
    close_prices: Union[np.ndarray, Sequence[float]],
    initial_capital: float = 100_000.0,
    commission: float = 0.0025,
    slippage: float = 0.0002,
) -> EquityStats:
    c = np.asarray(close_prices, dtype=np.float64)
    if c.size < 2 or c[0] <= 0:
        return EquityStats(
            total_pnl=0.0,
            final_equity=float(initial_capital),
            mdd=0.0,
            max_drawdown_pct=0.0,
            equity=c,
        )

    buy_mult = 1.0 + commission + slippage
    sell_mult = 1.0 - commission - slippage
    shares = float(initial_capital) / (float(c[0]) * buy_mult)
    eq_mtm = shares * c
    curve_mdd = equity_curve_stats(eq_mtm, initial_capital)

    final_cash = shares * float(c[-1]) * sell_mult
    total_pnl = final_cash / float(initial_capital) - 1.0

    return EquityStats(
        total_pnl=total_pnl,
        final_equity=final_cash,
        mdd=curve_mdd.mdd,
        max_drawdown_pct=curve_mdd.max_drawdown_pct,
        equity=eq_mtm,
    )


def objective_pnl_times_one_minus_mdd_pow(
    total_pnl: float,
    mdd: float,
    power: float = 1.0,
) -> float:
    om = 1.0 - float(mdd)
    return float(total_pnl) * (om ** float(power))


def annualized_cagr_trading_days(
    total_pnl_fractional: float,
    n_trading_days: int,
    *,
    trading_days_per_year: float = 252.0,
) -> float:
    """Compound annualized return over ``n_trading_days``: (1+R)^(252/n) - 1."""
    if n_trading_days < 1:
        return float("nan")
    growth = 1.0 + float(total_pnl_fractional)
    if growth <= 0:
        return float("nan")
    return growth ** (float(trading_days_per_year) / float(n_trading_days)) - 1.0
