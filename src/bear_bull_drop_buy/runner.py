"""Parallel grid execution."""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path
from typing import Any, List, Optional, Tuple

from bear_bull_drop_buy.backtest import run_backtest
from bear_bull_drop_buy.data_loader import DEFAULT_WARMUP_BARS, load_period
from bear_bull_drop_buy.grid import (
    expand_grid_efficient,
    grid_expand_stats,
    load_grid_json,
    merge_into_params_template,
    prepare_grid,
)
from bear_bull_drop_buy.metrics import objective_pnl_times_one_minus_mdd_pow
from bear_bull_drop_buy.params import StrategyParams


def default_grid_run_dir_name(ticker: str, period: str) -> str:
    """Filesystem-safe folder name: {ticker}_{start_end} (period ':' -> '_')."""
    return f"{ticker.strip()}_{period.strip().replace(':', '_')}"


def _run_one(
    args: Tuple[str, str, str, int, dict[str, Any], float, float],
) -> dict[str, Any]:
    ticker, period, db_path, warmup, param_dict, initial_capital, obj_power = args
    _, df_eval = load_period(db_path, ticker, period, warmup_bars=int(warmup))
    if df_eval.empty or len(df_eval) < 5:
        return {"ok": False, "error": "empty_or_short_eval_df", "params": param_dict}
    try:
        p = StrategyParams.from_dict(param_dict)
        p.validate()
    except Exception as e:
        return {"ok": False, "error": str(e), "params": param_dict}
    res = run_backtest(df_eval, p, initial_capital=initial_capital)
    s = res.stats
    score = objective_pnl_times_one_minus_mdd_pow(s.total_pnl, s.mdd, power=obj_power)
    out = {
        "ok": True,
        "params": param_dict,
        "total_pnl": s.total_pnl,
        "final_equity": s.final_equity,
        "mdd": s.mdd,
        "max_drawdown_pct": s.max_drawdown_pct,
        "objective_score": score,
    }
    return out


def run_grid(
    *,
    ticker: str,
    period: str,
    db_path: str,
    grid_path: str,
    params_template: Optional[dict[str, Any]] = None,
    initial_capital: float = 100_000.0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    workers: int = 0,
    objective_power: float = 1.0,
    out_dir: Optional[str] = None,
) -> Path:
    raw = load_grid_json(grid_path)
    axes, cost_overrides = prepare_grid(raw)
    template = (
        merge_into_params_template(StrategyParams().to_dict(), params_template)
        if params_template is not None
        else StrategyParams().to_dict()
    )
    template = merge_into_params_template(template, cost_overrides)
    combos: List[dict[str, Any]] = []
    for ov in expand_grid_efficient(axes):
        combos.append(merge_into_params_template(template, ov))

    if not combos:
        raise ValueError("grid produced zero combinations (check grid JSON lists)")

    expand_stats = grid_expand_stats(axes)

    root = Path(__file__).resolve().parents[2]
    rel_name = default_grid_run_dir_name(ticker, period)
    out_root = Path(out_dir) if out_dir else root / "var" / "grid_runs" / rel_name
    out_root.mkdir(parents=True, exist_ok=True)

    meta = {
        "ticker": ticker,
        "period": period,
        "db_path": db_path,
        "warmup_bars": warmup_bars,
        "objective_power": objective_power,
        **expand_stats,
    }
    (out_root / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    worker_args = [
        (ticker, period, db_path, warmup_bars, c, initial_capital, objective_power) for c in combos
    ]

    n_workers = workers if workers > 0 else max(1, mp.cpu_count() - 1)

    results: List[dict[str, Any]] = []
    if n_workers == 1:
        for a in worker_args:
            results.append(_run_one(a))
    else:
        with mp.Pool(processes=n_workers) as pool:
            for r in pool.imap_unordered(_run_one, worker_args, chunksize=max(1, len(worker_args) // (n_workers * 4) or 1)):
                results.append(r)

    ok = [r for r in results if r.get("ok")]
    ok.sort(key=lambda r: float(r.get("objective_score", -1e18)), reverse=True)
    (out_root / "results_sorted.json").write_text(json.dumps(ok, indent=2), encoding="utf-8")
    (out_root / "results_all.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    jsonl_path = out_root / "results_all.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as jf:
        for r in results:
            jf.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    if ok:
        (out_root / "best_params.json").write_text(json.dumps(ok[0]["params"], indent=2), encoding="utf-8")

    return out_root
