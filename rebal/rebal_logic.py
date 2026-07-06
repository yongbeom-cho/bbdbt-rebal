"""
Drift-based cash-only rebalancing for bbdbt-rebal.

Trigger: any ticker's weight deviates >= threshold (default 15%p) from 1/N.
Mechanics: transfer cash only from overweight → underweight tickers.
            No additional stock sells — strategy lots are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


def check_drift(
    equity_by_ticker: dict[str, float],
    tickers: list[str],
    threshold: float = 0.15,
) -> tuple[bool, dict[str, float], dict[str, float]]:
    """Return (should_rebal, weights, excess_by_ticker)."""
    n = len(tickers)
    total = sum(equity_by_ticker.get(t, 0.0) for t in tickers)
    if total <= 0 or n == 0:
        return False, {}, {}
    target = 1.0 / n
    weights = {t: equity_by_ticker.get(t, 0.0) / total for t in tickers}
    max_drift = max(abs(w - target) for w in weights.values())
    excess = {t: equity_by_ticker.get(t, 0.0) - total / n for t in tickers}
    return max_drift >= threshold, weights, excess


@dataclass
class RebalEvent:
    date: str
    weights_before: Dict[str, float]
    weights_after: Dict[str, float]
    transfers: Dict[str, float]  # ticker → net cash change (+recv / -given)
    cash_moved: float


def execute_cash_only_rebal(
    *,
    cash_by_ticker: dict[str, float],
    equity_by_ticker: dict[str, float],
    tickers: list[str],
    date: str,
) -> tuple[RebalEvent | None, dict[str, float]]:
    """
    Move cash from overweight tickers to underweight tickers.
    Giver transfer capped at available cash (no stock sells).
    Mutates cash_by_ticker in-place.
    """
    n = len(tickers)
    total = sum(equity_by_ticker[t] for t in tickers)
    if total <= 0:
        return None, equity_by_ticker

    target_each = total / n
    weights_before = {t: equity_by_ticker[t] / total for t in tickers}
    excess = {t: equity_by_ticker[t] - target_each for t in tickers}

    givers = [t for t in tickers if excess[t] > 0 and cash_by_ticker.get(t, 0.0) > 1e-9]
    receivers = [t for t in tickers if excess[t] < -1e-9]
    if not givers or not receivers:
        return None, equity_by_ticker

    transfers: dict[str, float] = {t: 0.0 for t in tickers}
    cash_pool = 0.0

    for t in givers:
        give = min(excess[t], cash_by_ticker[t])
        if give <= 1e-9:
            continue
        cash_by_ticker[t] -= give
        cash_pool += give
        transfers[t] -= give

    if cash_pool <= 1e-9:
        return None, equity_by_ticker

    total_deficit = sum(-excess[t] for t in receivers)
    for t in receivers:
        deficit = -excess[t]
        frac = deficit / total_deficit if total_deficit > 1e-12 else 1.0 / len(receivers)
        recv = cash_pool * frac
        cash_by_ticker[t] += recv
        transfers[t] += recv

    eq_after = dict(equity_by_ticker)
    for t in tickers:
        eq_after[t] = eq_after[t] + transfers[t]

    total_after = sum(eq_after.values())
    weights_after = {
        t: eq_after[t] / total_after if total_after > 0 else 1.0 / n
        for t in tickers
    }

    return RebalEvent(
        date=date,
        weights_before=weights_before,
        weights_after=weights_after,
        transfers=transfers,
        cash_moved=cash_pool,
    ), eq_after
