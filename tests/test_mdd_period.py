"""Drawdown period helpers (top-k episodes)."""

import pytest

from bear_bull_drop_buy.metrics import (
    equity_curve_stats,
    max_drawdown_period_indices,
    top_drawdown_period_indices,
)


def test_mdd_period_peak_to_trough():
    eq = [1.0, 1.2, 1.1, 0.9, 0.95, 1.3, 1.0, 0.8, 1.0]
    idxs = max_drawdown_period_indices(eq)
    assert idxs is not None
    peak_i, trough_i = idxs
    assert peak_i == 5
    assert trough_i == 7
    stats = equity_curve_stats(eq, 1.0)
    assert stats.mdd == pytest.approx(1.0 - 0.8 / 1.3)


def test_top_three_drawdowns():
    eq = [1.0, 1.2, 1.1, 0.9, 0.95, 1.3, 1.0, 0.8, 1.0, 1.1, 0.95, 0.85, 1.0]
    tops = top_drawdown_period_indices(eq, k=3)
    assert len(tops) >= 2
    depths = []
    for peak_i, trough_i in tops:
        depths.append(1.0 - eq[trough_i] / eq[peak_i])
    assert depths == sorted(depths, reverse=True)


def test_mdd_period_none_when_flat():
    assert max_drawdown_period_indices([1.0, 1.0, 1.0]) is None
    assert top_drawdown_period_indices([1.0, 1.0, 1.0], k=3) == []
