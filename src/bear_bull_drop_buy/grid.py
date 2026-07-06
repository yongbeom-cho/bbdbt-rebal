"""Cartesian grid from JSON: one flat map of parameters (list or min–max–step)."""

from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
from typing import Any, Dict, Iterator, List, Tuple

# Never Cartesian-swept; only a single scalar override allowed in JSON.
_COST_KEYS = frozenset({"commission", "slippage"})

# Ignored if present in grid JSON (removed feature).
_GRID_LEGACY_SKIP_KEYS = frozenset({
    "sell_all_cont_bear_day",
    "drop_easy_sell_enabled",
    "strategy_mode",
    "bear_easy_sell_bear_days",
})


def load_grid_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def expand_axis_to_list(v: Any) -> List[Any]:
    """
    - Explicit list: returned as-is (shallow copy).
    - Single scalar: [v].
    - Dict with min, max, step: arithmetic sequence from min to max inclusive.
      Integer axis if min, max, step are all int; otherwise float.
    """
    if isinstance(v, list):
        return list(v)
    if isinstance(v, dict) and "min" in v and "max" in v and "step" in v:
        lo, hi, st = v["min"], v["max"], v["step"]
        if lo == hi:
            if type(lo) is int and type(hi) is int:
                return [int(lo)]
            return [float(lo)] if isinstance(lo, (int, float)) else [lo]
        if st == 0:
            raise ValueError("axis step must be non-zero")
        if lo > hi:
            raise ValueError(f"axis min {lo!r} must be <= max {hi!r}")
        use_int = isinstance(lo, int) and isinstance(hi, int) and isinstance(st, int)
        out: List[Any] = []
        if use_int:
            x = int(lo)
            hi_i = int(hi)
            st_i = int(st)
            if st_i < 0:
                raise ValueError("negative step not supported for int axes")
            while x <= hi_i:
                out.append(x)
                x += st_i
        else:
            x = float(lo)
            hi_f = float(hi)
            st_f = float(st)
            if st_f < 0:
                raise ValueError("negative step not supported")
            eps = 1e-9
            n_guard = 0
            while x <= hi_f + eps:
                out.append(float(x))
                x += st_f
                n_guard += 1
                if n_guard > 1_000_000:
                    raise ValueError("axis range produced too many points (check min/max/step)")
        if not out:
            raise ValueError(f"axis produced empty list: {v!r}")
        return out
    return [v]


def _is_range_dict(v: Any) -> bool:
    return isinstance(v, dict) and "min" in v and "max" in v and "step" in v


def flatten_grid_document(raw: dict[str, Any]) -> dict[str, Any]:
    """
    One parameter map.

    - Preferred: flat JSON, every key is a StrategyParams field (sweep or scalar).
    - Legacy: { "defaults": {...}, "axes": {...} } merged into one map (axes wins on key clash).
    """
    if isinstance(raw.get("axes"), dict):
        merged: dict[str, Any] = {}
        for blk in (raw.get("defaults"), raw.get("strategy_defaults")):
            if isinstance(blk, dict):
                merged.update(blk)
        merged.update(raw["axes"])
        return merged
    return {k: v for k, v in raw.items() if k not in ("defaults", "strategy_defaults", "axes")}


def axes_and_fixed_costs(merged: dict[str, Any]) -> Tuple[dict[str, Any], dict[str, Any]]:
    """
    Split merged grid map into Cartesian axes vs fixed commission/slippage.

    commission and slippage may only appear as a single scalar each (no list, no min/max/step).
    If omitted, fixed map has no entry (StrategyParams defaults apply).
    """
    axes: dict[str, Any] = {}
    fixed: dict[str, Any] = {}
    for k, v in merged.items():
        if k.startswith("_"):
            continue
        if k in _GRID_LEGACY_SKIP_KEYS:
            continue
        if k in _COST_KEYS:
            if isinstance(v, list) or _is_range_dict(v):
                raise ValueError(
                    f"{k!r} is not swept: use a single number, or omit to use StrategyParams defaults"
                )
            fixed[k] = v
            continue
        axes[k] = v
    return axes, fixed


def prepare_grid(raw: dict[str, Any]) -> Tuple[dict[str, Any], dict[str, Any]]:
    """flatten_grid_document + axes_and_fixed_costs."""
    return axes_and_fixed_costs(flatten_grid_document(raw))


def expand_grid_naive(axes: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Full Cartesian product (may include redundant easy-sell=true bear sweeps)."""
    keys = sorted(axes.keys())
    axis_lists: List[List[Any]] = [expand_axis_to_list(axes[k]) for k in keys]
    for combo in itertools.product(*axis_lists):
        yield dict(zip(keys, combo))


def expand_grid_efficient(axes: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from expand_grid_naive(axes)


def expand_grid(axes: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield from expand_grid_naive(axes)


def count_grid_naive(axes: dict[str, Any]) -> int:
    return sum(1 for _ in expand_grid_naive(axes))


def count_grid(axes: dict[str, Any]) -> int:
    return sum(1 for _ in expand_grid_efficient(axes))


def grid_expand_stats(axes: dict[str, Any]) -> dict[str, int]:
    n_naive = count_grid_naive(axes)
    n_eff = count_grid(axes)
    return {
        "n_combos_naive": n_naive,
        "n_combos": n_eff,
        "n_saved_sell_all_cont_bear_redundant": 0,
        "n_saved_easy_sell_bear_redundant": 0,
    }


def count_grid_document(raw: dict[str, Any]) -> int:
    axes, _ = prepare_grid(raw)
    return count_grid(axes)


def merge_into_params_template(template: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(template)
    out.update(overrides)
    return out
