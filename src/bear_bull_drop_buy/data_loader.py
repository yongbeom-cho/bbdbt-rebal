"""SQLite OHLCV loader (usaetf day bars)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

DEFAULT_WARMUP_BARS = 300


def _convert_date_for_query(date_str: str, is_end: bool = False) -> str:
    if not date_str:
        return date_str
    if "-" not in date_str:
        return date_str
    date_compact = date_str.replace("-", "")
    return date_compact + ("2359" if is_end else "0000")


def _parse_db_dates(series: pd.Series) -> pd.DatetimeIndex:
    s = series.astype(str).str.strip()
    return pd.to_datetime(s.str.slice(0, 8), format="%Y%m%d", errors="coerce")


def load_ohlcv(
    db_path: str,
    ticker: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> pd.DataFrame:
    conn = sqlite3.connect(db_path)
    try:
        q = """
            SELECT date, open, high, low, close, volume
            FROM usaetf_ohlcv_day
            WHERE ticker = ?
        """
        params: List = [ticker]
        if start_date:
            q += " AND date >= ?"
            params.append(_convert_date_for_query(start_date, is_end=False))
        if end_date:
            q += " AND date <= ?"
            params.append(_convert_date_for_query(end_date, is_end=True))
        q += " ORDER BY date ASC"
        df = pd.read_sql(q, conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return df

    df.index = _parse_db_dates(df["date"])
    df = df.drop(columns=["date"])
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0.0)
    return df.dropna(subset=["open", "high", "low", "close"])


def default_db_path(root_dir: str) -> str:
    """Join root_dir with var/data/usaetf_ohlcv_day.db (root_dir = repo root)."""
    return os.path.join(root_dir, "var", "data", "usaetf_ohlcv_day.db")


def default_project_db_path() -> str:
    """Bundled DB: var/data/usaetf_ohlcv_day.db under this repo root."""
    root = Path(__file__).resolve().parents[2]
    return str(root / "var" / "data" / "usaetf_ohlcv_day.db")


def parse_period_arg(period: str) -> Tuple[pd.Timestamp, pd.Timestamp]:
    s = period.strip()
    if ":" not in s:
        raise ValueError(f"period must be 'start:end': {period!r}")
    a, b = s.split(":", 1)
    start = pd.Timestamp(a.strip()).normalize()
    end = pd.Timestamp(b.strip()).normalize()
    if pd.isna(start) or pd.isna(end):
        raise ValueError(f"failed to parse period: {period!r}")
    if end < start:
        raise ValueError(f"period end before start: {period!r}")
    return start, end


def business_days_in_period(period: str) -> int:
    """Count of business days from period start through end (inclusive), via pandas."""
    eval_start, eval_end = parse_period_arg(period)
    br = pd.bdate_range(eval_start.normalize(), eval_end.normalize(), freq="B")
    return int(len(br))


def load_period(
    db_path: str,
    ticker: str,
    period: str,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    eval_start, eval_end = parse_period_arg(period)
    load_start = eval_start - pd.tseries.offsets.BDay(int(warmup_bars))
    end_s = eval_end.strftime("%Y-%m-%d")
    df_full = load_ohlcv(db_path, ticker, load_start.strftime("%Y-%m-%d"), end_s)
    if df_full.empty:
        return df_full, df_full
    mask = (df_full.index >= eval_start) & (df_full.index <= eval_end)
    df_eval = df_full.loc[mask].copy()
    return df_full, df_eval
