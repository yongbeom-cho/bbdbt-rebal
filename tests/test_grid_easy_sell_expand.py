"""Grid: legacy sell_all / easy_sell keys are ignored; expansion is naive product."""

from __future__ import annotations

import itertools

from bear_bull_drop_buy.grid import axes_and_fixed_costs, expand_grid, grid_expand_stats


def test_legacy_sell_all_keys_skipped():
    cfg = {
        "regime_ma_type": ["ema"],
        "d_interval": [5],
        "period": [4],
        "sell_all_cont_bear_day": [0, 3],
        "drop_easy_sell_enabled": [True, False],
        "bear_take_profit_pct": [0.05, 0.06],
        "bull_take_profit_pct": [0.09],
        "bear_day_drop_buy_pct": [0.1],
        "bull_day_drop_buy_pct": [0.003],
        "bear_equity_buy_frac": [0.5],
        "bull_equity_buy_frac": [0.2],
        "bear_day_surge_partial_exit_pct": [0.03],
        "bull_day_surge_partial_exit_pct": [0.03],
        "bear_day_surge_sell_newest_n": [2],
        "bull_day_surge_sell_newest_n": [2],
    }
    axes, fixed = axes_and_fixed_costs(cfg)
    assert "sell_all_cont_bear_day" not in axes
    assert "drop_easy_sell_enabled" not in axes
    rows = list(expand_grid(axes))
    keys = sorted(axes.keys())
    axis_lists = [axes[k] for k in keys]
    assert len(rows) == len(list(itertools.product(*axis_lists)))
    stats = grid_expand_stats(axes)
    assert stats["n_combos"] == stats["n_combos_naive"]
    assert stats["n_saved_sell_all_cont_bear_redundant"] == 0
