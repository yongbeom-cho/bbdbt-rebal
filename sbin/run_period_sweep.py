#!/usr/bin/env python3
"""Random 2-year period sweep across tickers and configs.

Runs run_backtest() in-process (no CLI overhead) for:
  - N_PERIODS randomly sampled 2-year (start, end) windows (same windows for every config)
  - 12 tickers: LEV2QQQ, LEV3QQQ, LEV2SOXX, LEV3SOXX, TQQQ, QLD, SOXL, USD,
    TECL, FAS, CURE, DOW
  - auto_trader/config1-7.json (legacy wma_period keys are mapped via StrategyParams.from_dict)

Reports annualized_pnl (CAGR) and MDD: mean, min, max per (ticker, config).
"""

from __future__ import annotations

import json
import random
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bear_bull_drop_buy.backtest import run_backtest
from bear_bull_drop_buy.data_loader import default_project_db_path, load_ohlcv
from bear_bull_drop_buy.metrics import annualized_cagr_trading_days
from bear_bull_drop_buy.params import StrategyParams

# ── constants ────────────────────────────────────────────────────────────────
TICKERS = [
    "LEV2QQQ",
    "LEV3QQQ",
    "LEV2SOXX",
    "LEV3SOXX",
    "TQQQ",
    "QLD",
    "SOXL",
    "USD",
    "TECL",
    "FAS",
    "CURE",
    "DOW",
]

CONFIG_DIR = _ROOT / "auto_trader"
CONFIG_FILES = [
    "config1.json",
    "config2.json",
    "config3.json",
    "config4.json",
    "config5.json",
    "config6.json",
    "config7.json",
]

PERIOD_YEARS = 2
N_PERIODS = 500
CAPITAL = 100_000.0
WARMUP = 300
MIN_EVAL_BARS = 400  # skip periods with fewer eval trading days

# Sampling range: most tickers from 2010-03-11; DOW from 2019 (short windows skipped)
SAMPLE_START = date(2010, 3, 11)
SAMPLE_LAST_START = date(2024, 4, 10)   # end = start + 730 days ≤ 2026-04-10

SEED = 42


# ── helpers ───────────────────────────────────────────────────────────────────

def load_config_params(config_path: Path) -> StrategyParams:
    """Load strategy_params; legacy configs use wma_period (defaults to wma)."""
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    raw = dict(cfg.get("strategy_params", cfg))
    # StrategyParams.from_dict maps wma_period -> period and defaults regime_ma_type to wma
    return StrategyParams.from_dict(raw)


def generate_periods(n: int) -> list[tuple[date, date]]:
    """Generate n random 2-year (start, end) windows within the sample range."""
    total_days = (SAMPLE_LAST_START - SAMPLE_START).days
    rng = random.Random(SEED)
    offsets = sorted(rng.sample(range(total_days + 1), min(n, total_days + 1)))
    while len(offsets) < n:
        offsets.append(rng.randint(0, total_days))
    offsets = sorted(offsets[:n])
    periods = []
    for off in offsets:
        s = SAMPLE_START + timedelta(days=off)
        e = s + timedelta(days=PERIOD_YEARS * 365)
        periods.append((s, e))
    return periods


def slice_df(df_full: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    ts_s = pd.Timestamp(start)
    ts_e = pd.Timestamp(end)
    return df_full.loc[(df_full.index >= ts_s) & (df_full.index <= ts_e)].copy()


def fmt_pct(v: float) -> str:
    return f"{v * 100:+.2f}%"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    db_path = default_project_db_path()

    print(f"Loading configs from {CONFIG_DIR} ...")
    configs: list[tuple[str, StrategyParams]] = []
    for fname in CONFIG_FILES:
        p = CONFIG_DIR / fname
        params = load_config_params(p)
        configs.append((fname, params))
    print(f"  {len(configs)} configs loaded.")

    print(f"\nGenerating {N_PERIODS} random 2-year periods ...")
    periods = generate_periods(N_PERIODS)
    print(f"  Range: {periods[0][0]} → {periods[-1][1]}")

    print(f"\nLoading price data for {len(TICKERS)} tickers ...")
    ticker_data: dict[str, pd.DataFrame] = {}
    for ticker in TICKERS:
        df = load_ohlcv(db_path, ticker)
        ticker_data[ticker] = df
        print(f"  {ticker}: {len(df)} bars  ({df.index[0].date()} – {df.index[-1].date()})")

    # results[ticker][config_name] = list of (annualized_pnl, mdd)
    results: dict[str, dict[str, list[tuple[float, float]]]] = {
        t: {c: [] for c, _ in configs} for t in TICKERS
    }

    total_combos = N_PERIODS * len(TICKERS) * len(configs)
    print(f"\nRunning {total_combos:,} backtests ({N_PERIODS} periods × {len(TICKERS)} tickers × {len(configs)} configs)...")
    t0 = time.time()
    done = 0
    skipped = 0

    for i, (s, e) in enumerate(periods):
        for ticker in TICKERS:
            df_eval = slice_df(ticker_data[ticker], s, e)
            if len(df_eval) < MIN_EVAL_BARS:
                skipped += len(configs)
                done += len(configs)
                continue

            n_days = len(df_eval)
            for cfg_name, params in configs:
                try:
                    res = run_backtest(df_eval, params, initial_capital=CAPITAL)
                    s_stats = res.stats
                    cagr = annualized_cagr_trading_days(s_stats.total_pnl, n_days)
                    results[ticker][cfg_name].append((cagr, s_stats.mdd))
                except Exception:
                    skipped += 1
                done += 1

        if (i + 1) % 50 == 0 or (i + 1) == N_PERIODS:
            elapsed = time.time() - t0
            rate = done / elapsed
            remaining = (total_combos - done) / rate if rate > 0 else 0
            print(f"  [{i+1}/{N_PERIODS} periods]  {done:,} runs done  "
                  f"({elapsed:.0f}s elapsed, ~{remaining:.0f}s remaining)")

    elapsed = time.time() - t0
    print(f"\nCompleted {done:,} runs in {elapsed:.1f}s  (skipped={skipped:,})")

    # ── report ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"{'TICKER':<12} {'CONFIG':<12} {'N':>5}  "
          f"{'CAGR_mean':>10} {'CAGR_min':>10} {'CAGR_max':>10}  "
          f"{'MDD_mean':>9} {'MDD_min':>9} {'MDD_max':>9}")
    print("=" * 90)

    summary = {}
    for ticker in TICKERS:
        for cfg_name, _ in configs:
            vals = results[ticker][cfg_name]
            if not vals:
                print(f"{ticker:<12} {cfg_name:<12} {'0':>5}  {'N/A':>10}")
                continue
            cagrs = [v[0] for v in vals if not np.isnan(v[0])]
            mdds  = [v[1] for v in vals]
            if not cagrs:
                continue
            row = {
                "n": len(cagrs),
                "cagr_mean": float(np.mean(cagrs)),
                "cagr_min":  float(np.min(cagrs)),
                "cagr_max":  float(np.max(cagrs)),
                "mdd_mean":  float(np.mean(mdds)),
                "mdd_min":   float(np.min(mdds)),
                "mdd_max":   float(np.max(mdds)),
            }
            summary.setdefault(ticker, {})[cfg_name] = row
            print(
                f"{ticker:<12} {cfg_name:<12} {row['n']:>5}  "
                f"{fmt_pct(row['cagr_mean']):>10} {fmt_pct(row['cagr_min']):>10} {fmt_pct(row['cagr_max']):>10}  "
                f"{fmt_pct(row['mdd_mean']):>9} {fmt_pct(row['mdd_min']):>9} {fmt_pct(row['mdd_max']):>9}"
            )
        print()

    # save JSON summary
    out_path = _ROOT / "results" / "period_sweep_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"\nSummary saved to {out_path}")


if __name__ == "__main__":
    main()
