"""Viewer eval-window equity rebase (strategy starts at initial_capital)."""

from __future__ import annotations

import sys

import pytest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "viewer" / "api"))

from service import _rebase_equity_for_eval_window  # noqa: E402


def test_rebase_first_bar_equals_initial_capital():
    scaled, stats = _rebase_equity_for_eval_window([1.25, 1.5, 1.2], 1.0)
    assert scaled[0] == 1.0
    assert scaled[1] == pytest.approx(1.2)
    assert stats.final_equity == pytest.approx(0.96)
    assert stats.total_pnl == pytest.approx(-0.04)


def test_rebase_empty():
    scaled, stats = _rebase_equity_for_eval_window([], 1.0)
    assert scaled == []
    assert stats.final_equity == 1.0
