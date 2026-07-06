"""Bull/bear regime and drop easy-sell gate from subsampled SMA/WMA/EMA slope on close."""

from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from bear_bull_drop_buy.params import StrategyParams

_REGIME_MA_TYPES = frozenset({"sma", "wma", "ema"})

def _ema_last(seg: np.ndarray, span: int) -> float:
    if len(seg) < 1 or span < 1:
        return float("nan")
    return float(pd.Series(seg, dtype=float).ewm(span=span, adjust=False).mean().iloc[-1])


def _sma_last(seg: np.ndarray) -> float:
    if len(seg) < 1 or np.any(np.isnan(seg)):
        return float("nan")
    return float(np.mean(seg))


def wma_slope(close_a: np.ndarray, interval: int, period: int) -> np.ndarray:
    """
    At day i, compare two WMAs on subsampled closes:
      wma_now  uses close[i - interval*(period-1) : i+1 : interval]
      wma_prev uses close[i - interval*period     : i   : interval]
    slope = wma_now - wma_prev. Valid from i >= interval * period.
    """
    n = len(close_a)
    out = np.full(n, np.nan, dtype=float)
    if period < 1 or interval < 1:
        return out
    w = np.arange(1, period + 1, dtype=float)
    wsum = float(w.sum())
    min_i = interval * period
    for i in range(min_i, n):
        seg_now = close_a[i - interval * (period - 1) : i + 1 : interval]
        seg_prev = close_a[i - interval * period : i : interval]
        if np.any(np.isnan(seg_now)) or np.any(np.isnan(seg_prev)):
            continue
        wma_now = float(np.dot(seg_now, w) / wsum)
        wma_prev = float(np.dot(seg_prev, w) / wsum)
        out[i] = wma_now - wma_prev
    return out


def sma_slope(close_a: np.ndarray, interval: int, period: int) -> np.ndarray:
    """Same subsample windows as wma_slope; slope = SMA(seg_now) - SMA(seg_prev)."""
    n = len(close_a)
    out = np.full(n, np.nan, dtype=float)
    if period < 1 or interval < 1:
        return out
    min_i = interval * period
    for i in range(min_i, n):
        seg_now = close_a[i - interval * (period - 1) : i + 1 : interval]
        seg_prev = close_a[i - interval * period : i : interval]
        if len(seg_now) != period or len(seg_prev) != period:
            continue
        if np.any(np.isnan(seg_now)) or np.any(np.isnan(seg_prev)):
            continue
        sma_now = _sma_last(seg_now)
        sma_prev = _sma_last(seg_prev)
        if np.isnan(sma_now) or np.isnan(sma_prev):
            continue
        out[i] = sma_now - sma_prev
    return out


def ema_slope(close_a: np.ndarray, interval: int, period: int) -> np.ndarray:
    """Same subsample windows as wma_slope; slope = EMA(span=period) now − prev."""
    n = len(close_a)
    out = np.full(n, np.nan, dtype=float)
    if period < 1 or interval < 1:
        return out
    min_i = interval * period
    for i in range(min_i, n):
        seg_now = close_a[i - interval * (period - 1) : i + 1 : interval]
        seg_prev = close_a[i - interval * period : i : interval]
        if np.any(np.isnan(seg_now)) or np.any(np.isnan(seg_prev)):
            continue
        ema_now = _ema_last(seg_now, period)
        ema_prev = _ema_last(seg_prev, period)
        if np.isnan(ema_now) or np.isnan(ema_prev):
            continue
        out[i] = ema_now - ema_prev
    return out


def _ma_last_on_segment(seg: np.ndarray, period: int, ma_type: str) -> float:
    kind = ma_type.lower().strip()
    if kind == "sma":
        return _sma_last(seg)
    if kind == "ema":
        return _ema_last(seg, period)
    w = np.arange(1, period + 1, dtype=float)
    return float(np.dot(seg, w) / w.sum())


def regime_ma_level_series(
    close_a: np.ndarray,
    interval: int,
    period: int,
    ma_type: str = "wma",
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Per-bar regime MA level on the current subsampled window (seg_now) and slope.
    Slope matches ``regime_slope`` / bull-bear (now MA minus previous window MA).
    """
    n = len(close_a)
    level = np.full(n, np.nan, dtype=float)
    slope = regime_slope(close_a, interval, period, ma_type)
    if period < 1 or interval < 1:
        return level, slope
    min_i = interval * period
    kind = ma_type.lower().strip()
    for i in range(min_i, n):
        seg_now = close_a[i - interval * (period - 1) : i + 1 : interval]
        if len(seg_now) != period or np.any(np.isnan(seg_now)):
            continue
        v = _ma_last_on_segment(seg_now, period, kind)
        if not np.isnan(v):
            level[i] = v
    return level, slope


def regime_slope(
    close_a: np.ndarray,
    interval: int,
    period: int,
    ma_type: str = "wma",
) -> np.ndarray:
    kind = ma_type.lower().strip()
    if kind not in _REGIME_MA_TYPES:
        raise ValueError(f"regime_ma_type must be sma, wma, or ema, got {ma_type!r}")
    if kind == "sma":
        return sma_slope(close_a, interval, period)
    if kind == "ema":
        return ema_slope(close_a, interval, period)
    return wma_slope(close_a, interval, period)


def bull_regime_by_day(
    df: pd.DataFrame,
    interval: int,
    period: int,
    ma_type: str = "wma",
) -> Tuple[np.ndarray, int]:
    """True when regime MA slope > 0 (bull)."""
    n = len(df)
    flags = np.zeros(n, dtype=bool)
    close_a = df["close"].to_numpy(dtype=float)
    slope = regime_slope(close_a, interval, period, ma_type)
    min_valid_i = interval * period
    for i in range(min_valid_i, n):
        if np.isnan(slope[i]):
            continue
        flags[i] = slope[i] > 0.0
    return flags, min_valid_i


def regime_slope_positive_last_bar(
    df: pd.DataFrame,
    interval: int,
    period: int,
    ma_type: str = "wma",
) -> bool:
    flags, min_i = bull_regime_by_day(df, interval, period, ma_type)
    i = len(df) - 1
    if i < min_i:
        return False
    return bool(flags[i])

