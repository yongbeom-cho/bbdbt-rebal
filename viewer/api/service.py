"""Backtest + DB helpers for the bear-bull drop-buy viewer API."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bear_bull_drop_buy.backtest import run_backtest
from bear_bull_drop_buy.regime import regime_ma_level_series
from bear_bull_drop_buy.data_loader import (
    DEFAULT_WARMUP_BARS,
    default_project_db_path,
    load_period,
    parse_period_arg,
)
from bear_bull_drop_buy.metrics import (
    annualized_cagr_trading_days,
    buy_and_hold_stats,
    equity_curve_stats,
    top_drawdown_period_indices,
)
from bear_bull_drop_buy.ohlcv_sync import sync_usaetf_ohlcv_if_needed, today_iso
from bear_bull_drop_buy.params import StrategyParams

_ROOT = Path(__file__).resolve().parents[2]
_AUTO_TRADER = _ROOT / "auto_trader"
_GRID_DEFAULT = _ROOT / "configs" / "grid_default.json"
_AUTO_TRADER_CONFIG = _ROOT / "auto_trader" / "config14.json"
_VIEWER_RUN_KEYS = frozenset({"ticker", "start", "end"})


def _discover_config_ids() -> list[str]:
    ids = []
    for path in _AUTO_TRADER.glob("config*.json"):
        stem = path.stem
        if stem[len("config"):].isdigit():
            ids.append(stem)
    return sorted(ids, key=lambda cid: int(cid[len("config"):]))


_CONFIG_IDS = _discover_config_ids()  # auto_trader/config<N>.json, discovered dynamically

# grid_default.json sweep keys (viewer strategy form)
_GRID_PARAM_KEYS = (
    "regime_ma_type",
    "d_interval",
    "period",
    "bear_take_profit_pct",
    "bull_take_profit_pct",
    "bear_day_drop_buy_pct",
    "bull_day_drop_buy_pct",
    "bear_equity_buy_frac",
    "bull_equity_buy_frac",
    "bear_day_surge_partial_exit_pct",
    "bull_day_surge_partial_exit_pct",
    "bear_day_surge_sell_newest_n",
    "bull_day_surge_sell_newest_n",
)

_BUY_KINDS = frozenset({"drop_buy"})
_OVERLAY_MA_TYPES = frozenset({"none", "sma", "ema", "wma"})


def _trade_pool(kind: str) -> str:
    return "drop"


def _iso_date(ts: pd.Timestamp) -> str:
    return ts.normalize().strftime("%Y-%m-%d")


def _bar_iso(idx: Any) -> str:
    if hasattr(idx, "date"):
        return idx.date().isoformat()
    return str(pd.Timestamp(idx).date())


def list_configs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cid in _CONFIG_IDS:
        path = _AUTO_TRADER / f"{cid}.json"
        if not path.is_file():
            continue
        with path.open(encoding="utf-8") as f:
            raw = json.load(f)
        tickers = [s["ticker"] for s in raw.get("strategies", []) if s.get("ticker")]
        p = raw.get("strategy_params", {})
        out.append({
            "id": cid,
            "label": cid.replace("config", "Config "),
            "path": str(path),
            "strategy_tickers": tickers,
            "regime_ma_type": p.get("regime_ma_type", "wma"),
            "period": p.get("period", p.get("wma_period")),
            "d_interval": p.get("d_interval"),
        })
    return out


def _grid_axis_mid(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[len(value) // 2]
    if isinstance(value, dict) and "min" in value and "max" in value:
        lo = float(value["min"])
        hi = float(value["max"])
        step = float(value.get("step", hi - lo))
        if step <= 0:
            return lo
        n = max(1, int(round((hi - lo) / step)))
        return lo + (n // 2) * step
    return value


def _load_auto_trader_config_raw() -> dict[str, Any]:
    if not _AUTO_TRADER_CONFIG.is_file():
        raise FileNotFoundError(_AUTO_TRADER_CONFIG)
    with _AUTO_TRADER_CONFIG.open(encoding="utf-8") as f:
        return json.load(f)


def load_grid_schema() -> dict[str, Any]:
    if not _GRID_DEFAULT.is_file():
        raise FileNotFoundError(_GRID_DEFAULT)
    with _GRID_DEFAULT.open(encoding="utf-8") as f:
        raw = json.load(f)
    axes: dict[str, Any] = {}
    for key in _GRID_PARAM_KEYS:
        if key in raw:
            axes[key] = raw[key]
    defaults_mid = {k: _grid_axis_mid(axes[k]) for k in axes}
    defaults_first = {
        k: (axes[k][0] if isinstance(axes[k], list) and axes[k] else _grid_axis_mid(axes[k]))
        for k in axes
    }

    cfg_raw = _load_auto_trader_config_raw()
    viewer_run: dict[str, Any] = {}
    for key in _VIEWER_RUN_KEYS:
        if key in cfg_raw:
            viewer_run[key] = cfg_raw[key]
    strategies = cfg_raw.get("strategies", [])
    if "ticker" not in viewer_run and strategies:
        t0 = strategies[0].get("ticker")
        if t0:
            viewer_run["ticker"] = t0
    viewer_run["end"] = today_iso()
    params_raw = cfg_raw.get("strategy_params", {})
    defaults = params_from_dict(params_raw).to_dict()

    return {
        "source": str(_GRID_DEFAULT.relative_to(_ROOT)),
        "viewer_default_source": str(_AUTO_TRADER_CONFIG.relative_to(_ROOT)),
        "axes": axes,
        "defaults": defaults,
        "viewer_run": viewer_run,
        "defaults_mid": defaults_mid,
        "defaults_first": defaults_first,
    }


def params_from_dict(raw: dict[str, Any]) -> StrategyParams:
    p = StrategyParams.from_dict(raw)
    p.validate()
    return p


def load_config_params(config_id: str) -> StrategyParams:
    if config_id not in _CONFIG_IDS:
        raise ValueError(f"unknown config: {config_id}")
    path = _AUTO_TRADER / f"{config_id}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    with path.open(encoding="utf-8") as f:
        raw = json.load(f)
    params_raw = raw.get("strategy_params", raw)
    return StrategyParams.from_dict(params_raw)


def ensure_ohlcv_current(db_path: str | None = None) -> dict[str, Any]:
    """Fill OHLCV from (last DB date + 1) through the latest US business day."""
    return sync_usaetf_ohlcv_if_needed(db_path)


def load_ohlcv_close(
    db_path: str,
    ticker: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    """Return [{time: YYYY-MM-DD, value: close}, ...] for ticker in date range."""
    from bear_bull_drop_buy.data_loader import load_ohlcv
    df = load_ohlcv(db_path, ticker, start_date, end_date)
    if df.empty:
        return []
    return [
        {"time": idx.strftime("%Y-%m-%d"), "value": float(row["close"])}
        for idx, row in df.iterrows()
    ]


def list_tickers(db_path: str | None = None) -> list[dict[str, Any]]:
    db = db_path or default_project_db_path()
    ensure_ohlcv_current(db)
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            """
            SELECT ticker,
                   MIN(SUBSTR(TRIM(date), 1, 8)) AS min_d,
                   MAX(SUBSTR(TRIM(date), 1, 8)) AS max_d,
                   COUNT(*) AS n_bars
            FROM usaetf_ohlcv_day
            GROUP BY ticker
            ORDER BY ticker
            """
        ).fetchall()
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for ticker, min_d, max_d, n_bars in rows:
        min_s = f"{min_d[:4]}-{min_d[4:6]}-{min_d[6:8]}"
        max_s = f"{max_d[:4]}-{max_d[4:6]}-{max_d[6:8]}"
        out.append({
            "ticker": ticker,
            "min_date": min_s,
            "max_date": max_s,
            "n_bars": int(n_bars),
        })
    return out


def _rebase_equity_for_eval_window(
    equity_values: list[float],
    initial_capital: float,
) -> tuple[list[float], Any]:
    """Scale eval-window equity so the first bar equals initial_capital."""
    if not equity_values:
        empty = np.array([], dtype=np.float64)
        return [], equity_curve_stats(empty, initial_capital)
    base = float(equity_values[0])
    if base <= 0:
        scaled = [float(v) for v in equity_values]
    else:
        factor = float(initial_capital) / base
        scaled = [float(v) * factor for v in equity_values]
    stats = equity_curve_stats(
        np.asarray(scaled, dtype=np.float64),
        float(initial_capital),
    )
    return scaled, stats


def _stats_payload(stats: Any, n_trading_days: int) -> dict[str, Any]:
    cagr = annualized_cagr_trading_days(stats.total_pnl, n_trading_days)
    cagr_out: float | None = float(cagr)
    if cagr_out != cagr_out:  # NaN
        cagr_out = None
    return {
        "total_pnl": float(stats.total_pnl),
        "final_equity": float(stats.final_equity),
        "mdd": float(stats.mdd),
        "max_drawdown_pct": float(stats.max_drawdown_pct),
        "cagr": cagr_out,
        "n_trading_days": int(n_trading_days),
    }


def _normalize_trade(ev: dict[str, Any]) -> dict[str, Any]:
    kind = str(ev.get("kind", ""))
    side = "buy" if kind in _BUY_KINDS else "sell"
    shares = float(ev.get("shares", 0))
    notional = float(ev.get("cost") or ev.get("proceeds") or 0.0)
    return {
        "date": ev["date"],
        "kind": kind,
        "pool": _trade_pool(kind),
        "side": side,
        "price": float(ev.get("price", 0)),
        "shares": shares,
        "notional": notional,
        "n_lots": int(ev.get("n_lots", 0)),
        "entry": ev.get("entry"),
        "day_ret": ev.get("day_ret"),
        "rung_index": ev.get("rung_index"),
    }


def _wma_now_at_index(close_a: np.ndarray, i: int, interval: int, period: int) -> float | None:
    """Strategy-style WMA (subsampled) value at bar index i."""
    w = np.arange(1, period + 1, dtype=float)
    wsum = float(w.sum())
    seg = close_a[i - interval * (period - 1) : i + 1 : interval]
    if len(seg) != period or np.any(np.isnan(seg)):
        return None
    return float(np.dot(seg, w) / wsum)


def _compute_ma_series(
    close: pd.Series,
    ma_type: str,
    period: int,
    *,
    wma_interval: int = 1,
) -> pd.Series:
    kind = ma_type.lower().strip()
    if kind == "none" or period < 1:
        return pd.Series(index=close.index, dtype=float)
    if kind == "sma":
        return close.rolling(period, min_periods=period).mean()
    if kind == "ema":
        return close.ewm(span=period, adjust=False).mean()
    if kind == "wma":
        interval = max(1, int(wma_interval))
        if interval == 1:
            weights = np.arange(1, period + 1, dtype=float)
            return close.rolling(period, min_periods=period).apply(
                lambda x: float(np.dot(x, weights) / weights.sum()),
                raw=True,
            )
        close_a = close.to_numpy(dtype=float)
        out = np.full(len(close_a), np.nan, dtype=float)
        min_i = interval * period
        for i in range(min_i, len(close_a)):
            v = _wma_now_at_index(close_a, i, interval, period)
            if v is not None:
                out[i] = v
        return pd.Series(out, index=close.index)
    raise ValueError(f"unknown overlay_ma: {ma_type!r} (use sma, ema, wma, none)")


def _build_overlay(
    df_full: pd.DataFrame,
    eval_start: pd.Timestamp,
    eval_end: pd.Timestamp,
    ma_type: str,
    period: int,
    wma_interval: int = 1,
) -> dict[str, Any] | None:
    kind = ma_type.lower().strip()
    if kind not in _OVERLAY_MA_TYPES or kind == "none":
        return None
    period = int(period)
    if period < 2 or period > 500:
        raise ValueError("overlay_period must be 2..500")
    interval = max(1, int(wma_interval))
    close_a = df_full["close"].to_numpy(dtype=float)
    level_a, slope_a = regime_ma_level_series(close_a, interval, period, kind)
    points: list[dict[str, Any]] = []
    label = kind.upper()
    for i, idx in enumerate(df_full.index):
        if idx < eval_start or idx > eval_end:
            continue
        raw = level_a[i]
        if np.isnan(raw):
            continue
        val = float(raw)
        s = slope_a[i]
        slope: str | None = None
        if not np.isnan(s):
            if s > 0:
                slope = "+"
            elif s < 0:
                slope = "-"
            else:
                slope = "0"
        points.append({
            "time": _bar_iso(idx),
            "value": val,
            "slope": slope,
        })
    if kind == "wma" and interval > 1:
        ma_label = f"WMA{period}(d{interval})"
    elif interval > 1:
        ma_label = f"{label}{period}(d{interval})"
    else:
        ma_label = f"{label}{period}"
    return {
        "type": kind,
        "period": period,
        "wma_interval": interval if interval > 1 else None,
        "label": ma_label,
        "points": points,
    }


def _regime_overlay_spec(params: StrategyParams) -> tuple[str, int, int]:
    """Chart overlay always matches strategy regime MA."""
    ma = params.regime_ma_type.lower().strip()
    if ma not in ("sma", "wma", "ema"):
        ma = "wma"
    return ma, int(params.period), max(1, int(params.d_interval))


def _eod_lots_by_date(
    debug_bars: list[dict[str, Any]] | None,
    eval_start: pd.Timestamp,
    eval_end: pd.Timestamp,
) -> dict[str, int]:
    """End-of-day lot counts (after all actions on that bar)."""
    out: dict[str, int] = {}
    if not debug_bars:
        return out
    for bar in debug_bars:
        d = str(bar["date"])
        ts = pd.Timestamp(d).normalize()
        if ts < eval_start or ts > eval_end:
            continue
        out[d] = int(bar["n_lots"])
    return out


def _eod_portfolio_by_date(
    debug_bars: list[dict[str, Any]] | None,
    eval_start: pd.Timestamp,
    eval_end: pd.Timestamp,
) -> dict[str, float]:
    """End-of-day cash ratio (cash / total_equity) by date."""
    out: dict[str, float] = {}
    if not debug_bars:
        return out
    for bar in debug_bars:
        d = str(bar["date"])
        ts = pd.Timestamp(d).normalize()
        if ts < eval_start or ts > eval_end:
            continue
        cash = float(bar.get("cash", 0))
        equity = float(bar.get("equity", 0))
        out[d] = cash / equity if equity > 0 else 1.0
    return out


def _aggregate_trades_for_chart(
    trades: list[dict[str, Any]],
    eod_lots_by_date: dict[str, int],
) -> list[dict[str, Any]]:
    """
    Chart markers: one per calendar day per side (buy/sell).
    Lot counts use end-of-day snapshot (not per-fill).
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for t in trades:
        if t["shares"] <= 0 or t["notional"] <= 0:
            continue
        key = (t["date"], t["side"])
        eod_lots = eod_lots_by_date.get(t["date"], t["n_lots"])
        if key not in buckets:
            buckets[key] = {
                "date": t["date"],
                "pool": "drop",
                "side": t["side"],
                "kinds": [],
                "price": t["price"],
                "shares": 0.0,
                "notional": 0.0,
                "n_fills": 0,
                "n_lots": int(eod_lots),
            }
        b = buckets[key]
        b["shares"] += t["shares"]
        b["notional"] += t["notional"]
        b["n_fills"] += 1
        if t["kind"] not in b["kinds"]:
            b["kinds"].append(t["kind"])
        b["price"] = t["price"]

    out: list[dict[str, Any]] = []
    for b in sorted(buckets.values(), key=lambda x: (x["date"], x["side"])):
        kinds: list[str] = b.pop("kinds")
        b["kind"] = kinds[0] if len(kinds) == 1 else "mixed"
        if len(kinds) > 1:
            b["kind_detail"] = kinds
        out.append(b)
    return out


def run_viewer_backtest(
    *,
    config_id: str | None = None,
    strategy_params: StrategyParams | dict[str, Any] | None = None,
    ticker: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 1.0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    db_path: str | None = None,
) -> dict[str, Any]:
    if strategy_params is not None:
        params = (
            strategy_params
            if isinstance(strategy_params, StrategyParams)
            else params_from_dict(strategy_params)
        )
        run_label = config_id or "custom"
    elif config_id:
        params = load_config_params(config_id)
        run_label = config_id
    else:
        raise ValueError("config_id or strategy_params required")
    db = db_path or default_project_db_path()
    ensure_ohlcv_current(db)
    period = f"{start_date}:{end_date}"
    eval_start, eval_end = parse_period_arg(period)

    df_full, df_eval = load_period(db, ticker, period, warmup_bars=warmup_bars)
    if df_eval.empty or len(df_eval) < 2:
        raise ValueError("no OHLCV data for ticker and date range")

    res = run_backtest(
        df_full,
        params,
        initial_capital=float(initial_capital),
        trade_log=True,
        debug_trace=True,
    )
    eod_lots = _eod_lots_by_date(res.debug_bars, eval_start, eval_end)
    eod_cash_ratio = _eod_portfolio_by_date(res.debug_bars, eval_start, eval_end)
    ov_ma, ov_period, ov_interval = _regime_overlay_spec(params)
    overlay = _build_overlay(
        df_full,
        eval_start,
        eval_end,
        ov_ma,
        ov_period,
        wma_interval=ov_interval,
    )
    overlay_by_time = {p["time"]: p for p in (overlay or {}).get("points", [])}

    ohlc: list[dict[str, Any]] = []
    eval_times: list[str] = []
    eval_equity_raw: list[float] = []
    closes = df_full["close"].to_numpy(dtype=float)
    for i, idx in enumerate(df_full.index):
        if idx < eval_start or idx > eval_end:
            continue
        t = _bar_iso(idx)
        row = df_full.iloc[i]
        ov = overlay_by_time.get(t)
        bar: dict[str, Any] = {
            "time": t,
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(closes[i]),
            "volume": float(row.get("volume", 0)),
        }
        if ov is not None:
            bar["overlay_value"] = ov["value"]
            bar["overlay_slope"] = ov.get("slope")
        cr = eod_cash_ratio.get(t)
        if cr is not None:
            bar["cash_ratio"] = round(cr, 6)
        ohlc.append(bar)
        eval_times.append(t)
        eval_equity_raw.append(float(res.equity_curve[i]))

    eval_equity_scaled, strat_stats = _rebase_equity_for_eval_window(
        eval_equity_raw,
        float(initial_capital),
    )
    equity = [
        {"time": t, "value": v}
        for t, v in zip(eval_times, eval_equity_scaled, strict=True)
    ]

    mdd_periods: list[dict[str, Any]] = []
    for rank, (peak_i, trough_i) in enumerate(
        top_drawdown_period_indices(eval_equity_scaled, k=3),
        start=1,
    ):
        peak_eq = float(eval_equity_scaled[peak_i])
        trough_eq = float(eval_equity_scaled[trough_i])
        dd_frac = (trough_eq / peak_eq - 1.0) if peak_eq > 0 else 0.0
        mdd_periods.append({
            "rank": rank,
            "start": eval_times[peak_i],
            "end": eval_times[trough_i],
            "peak_equity": peak_eq,
            "trough_equity": trough_eq,
            "drawdown_pct": float(-dd_frac) if dd_frac < 0 else 0.0,
        })
    mdd_period = mdd_periods[0] if mdd_periods else None

    trades: list[dict[str, Any]] = []
    for ev in res.trade_events or []:
        d = pd.Timestamp(ev["date"]).normalize()
        if d < eval_start or d > eval_end:
            continue
        trades.append(_normalize_trade(ev))

    n_days = len(df_eval)
    eval_closes = df_eval["close"].to_numpy(dtype=float)
    bh = buy_and_hold_stats(
        eval_closes,
        initial_capital=float(initial_capital),
        commission=float(params.commission),
        slippage=float(params.slippage),
    )

    buy_hold_equity: list[dict[str, Any]] = []
    if bh.equity is not None and len(bh.equity) == len(df_eval):
        for i, idx in enumerate(df_eval.index):
            buy_hold_equity.append({
                "time": _bar_iso(idx),
                "value": float(bh.equity[i]),
            })

    chart_trades = _aggregate_trades_for_chart(trades, eod_lots)
    drop_fills = len(trades)

    return {
        "config_id": run_label,
        "params": params.to_dict(),
        "ticker": ticker,
        "period": {"start": _iso_date(eval_start), "end": _iso_date(eval_end)},
        "warmup_bars": warmup_bars,
        "initial_capital": float(initial_capital),
        "stats": {
            "strategy": _stats_payload(strat_stats, n_days),
            "buy_hold": _stats_payload(bh, n_days),
        },
        "ohlc": ohlc,
        "overlay": overlay,
        "equity": equity,
        "mdd_period": mdd_period,
        "mdd_periods": mdd_periods,
        "buy_hold_equity": buy_hold_equity,
        "trades": trades,
        "chart_trades": chart_trades,
        "n_trades": len(trades),
        "n_chart_markers": len(chart_trades),
        "trade_breakdown": {
            "drop_fills": drop_fills,
            "lots_end": len(res.lots_final),
        },
    }
