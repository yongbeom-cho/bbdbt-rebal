"""Sync usaetf daily OHLCV gaps (FinanceDataReader) for all tickers in the local DB."""

from __future__ import annotations

import importlib.util
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from bear_bull_drop_buy.data_loader import default_project_db_path

logger = logging.getLogger(__name__)

TABLE = "usaetf_ohlcv_day"
DATE_FMT = "%Y%m%d0000"

# (underlying, synthetic_ticker, up_leverage, down_leverage)
# LEV3* uses asymmetric leverage — tuned to match observed real-world 3x leveraged
# ETF decay (e.g. SOXL, TQQQ) more closely than symmetric 3.0x:
#   - underlying is a real tradable ETF (GLD/QQQ/SOXX/SPY/KODEX200/KODEXSEMI): 2.95 up / 2.99 down
#   - underlying is an index/indicator series, not a tradable ETF (GOLD/SOX/NASDAQ): 2.95 up / 2.975 down
SYNTHETIC_SPECS: tuple[tuple[str, str, float, float], ...] = (
    ("GLD", "LEV2GLD", 2.0, 2.0),
    ("GLD", "LEV3GLD", 2.95, 2.99),
    ("QQQ", "LEV2QQQ", 2.0, 2.0),
    ("QQQ", "LEV3QQQ", 2.95, 2.99),
    ("SOXX", "LEV2SOXX", 2.0, 2.0),
    ("SOXX", "LEV3SOXX", 2.95, 2.99),
    ("SPY", "LEV2SPY", 2.0, 2.0),
    ("SPY", "LEV3SPY", 2.95, 2.99),
    ("KODEX200", "LEV3KODEX200", 2.95, 2.99),
    ("KODEXSEMI", "LEV3KODEXSEMI", 2.95, 2.99),
    ("GOLD", "LEV3GOLD", 2.95, 2.975),
    ("SOX", "LEV3SOX", 2.95, 2.975),
    ("NASDAQ", "LEV3NASDAQ", 2.95, 2.975),
)

# KRX tickers updated via pykrx, not FDR — skip FDR sync
_KRX_TICKERS = frozenset({"KODEX200", "KODEXSEMI"})

# FRED tickers synced via FRED public CSV, not FDR
# "GOLD" = London gold AM fix / 10 ≈ synthetic GLD price, sourced from FRED series GOLDAMGBD228NLBM
_FRED_TICKERS = frozenset({"GOLD"})
_FRED_SERIES: dict[str, tuple[str, float]] = {
    "GOLD": ("GOLDAMGBD228NLBM", 10.0),
}

# Yahoo Finance index tickers (no ETF wrapper, longer history than the tracking ETF)
_INDEX_TICKERS = frozenset({"SOX", "NASDAQ"})
_INDEX_SERIES: dict[str, str] = {
    "SOX": "^SOX",  # PHLX Semiconductor Sector Index (yfinance data from 1994-05-04)
    "NASDAQ": "^IXIC",  # NASDAQ Composite Index (yfinance data from 1971-02-05)
}

_SYNTHETIC_TICKERS = frozenset(s[1] for s in SYNTHETIC_SPECS)
_UNDERLYING_BY_SYNTHETIC = {s[1]: (s[0], s[2], s[3]) for s in SYNTHETIC_SPECS}


@dataclass(frozen=True)
class TickerGap:
    ticker: str
    db_max: pd.Timestamp
    gap_start: pd.Timestamp


def latest_us_business_day(as_of: pd.Timestamp | None = None) -> pd.Timestamp:
    """Last NYSE business day on or before *as_of* (defaults to today)."""
    ref = pd.Timestamp(as_of or pd.Timestamp.today()).normalize()
    return pd.bdate_range(end=ref, periods=1)[0]


def sync_target_end(as_of: pd.Timestamp | None = None) -> pd.Timestamp:
    """Last US business day before calendar today (skip in-progress session)."""
    ref = pd.Timestamp(as_of or pd.Timestamp.today()).normalize()
    through = ref - pd.Timedelta(days=1)
    return latest_us_business_day(through)


def today_iso() -> str:
    return pd.Timestamp.today().normalize().strftime("%Y-%m-%d")


def _parse_db_date(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    s = str(value).strip()
    if len(s) >= 8 and s[:8].isdigit():
        return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
    return pd.Timestamp(s).normalize()


def _load_db_tickers(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        f"SELECT DISTINCT ticker FROM {TABLE} ORDER BY ticker"
    ).fetchall()
    return [str(r[0]).upper() for r in rows]


def _ticker_max_date(conn: sqlite3.Connection, ticker: str) -> pd.Timestamp | None:
    row = conn.execute(
        f"SELECT MAX(date) FROM {TABLE} WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    return _parse_db_date(row[0] if row else None)


def _ticker_last_close(conn: sqlite3.Connection, ticker: str) -> float | None:
    row = conn.execute(
        f"SELECT close FROM {TABLE} WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


_MAX_SANE_DAILY_MOVE = 0.40


def _reject_implausible_bars(
    ticker: str,
    df: pd.DataFrame,
    prev_close: float | None,
    max_move: float = _MAX_SANE_DAILY_MOVE,
) -> pd.DataFrame | None:
    """Guard against single-day-request glitches (e.g. a data provider returning a
    phantom/placeholder bar for a day the market was actually closed, such as a
    holiday). Rejects the whole batch if any close-to-close move exceeds
    `max_move` — real index/commodity data essentially never jumps this much in
    one session, so a violation means the fetched bar is spurious, not real.
    """
    closes = list(df["close"].values)
    chain = ([prev_close] if prev_close is not None else []) + closes
    for a, b in zip(chain, chain[1:]):
        if a is None or a == 0:
            continue
        if abs(b / a - 1.0) > max_move:
            logger.warning(
                "%s: rejecting fetched bar(s) — implausible move %.1f%% (prev_close=%s, close=%s)",
                ticker, (b / a - 1.0) * 100, a, b,
            )
            return None
    return df


def plan_ticker_gaps(
    db_path: str | Path,
    *,
    as_of: pd.Timestamp | None = None,
) -> tuple[pd.Timestamp, list[TickerGap]]:
    """Per-ticker gaps from (db_max + 1) through sync_target_end (yesterday's last biz day)."""
    target_end = sync_target_end(as_of)
    path = Path(db_path)
    if not path.is_file():
        return target_end, []

    conn = sqlite3.connect(path)
    try:
        tickers = _load_db_tickers(conn)
        gaps: list[TickerGap] = []
        for ticker in tickers:
            if ticker in _SYNTHETIC_TICKERS or ticker in _KRX_TICKERS:
                continue
            db_max = _ticker_max_date(conn, ticker)
            if db_max is None:
                continue
            if db_max >= target_end:
                continue
            gaps.append(
                TickerGap(
                    ticker=ticker,
                    db_max=db_max,
                    gap_start=db_max + pd.Timedelta(days=1),
                )
            )
    finally:
        conn.close()
    return target_end, gaps


def _fdr_df_to_ohlcv(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or df.empty:
        return None
    need = {"Open", "High", "Low", "Close", "Volume"}
    if not need.issubset(set(df.columns)):
        return None
    out = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    if "Adj Close" in df.columns:
        adj = df["Adj Close"].astype(float)
        cl = df["Close"].astype(float)
        nf = adj / cl.replace(0, float("nan"))
        nf = nf.fillna(1.0)
        for c in ("Open", "High", "Low", "Close"):
            out[c] = out[c].astype(float) * nf
    out.columns = ["open", "high", "low", "close", "volume"]
    out["volume"] = out["volume"].fillna(0).astype(float)
    out.index = pd.to_datetime(out.index)
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    return out


def _fetch_fdr_usadj_ohlcv(
    code: str,
    str_start_dt: str,
    str_end_dt: str,
) -> pd.DataFrame | None:
    try:
        import FinanceDataReader as fdr
    except ImportError:
        raise ImportError("finance-datareader is required for OHLCV sync") from None

    try:
        raw = fdr.DataReader(code)
    except Exception as exc:
        logger.warning("FDR DataReader(%s) failed: %s", code, exc)
        return None

    ohlcv = _fdr_df_to_ohlcv(raw)
    if ohlcv is None or ohlcv.empty:
        return None

    start = pd.Timestamp(str_start_dt).normalize()
    end = pd.Timestamp(str_end_dt).normalize()
    ohlcv = ohlcv[(ohlcv.index >= start) & (ohlcv.index < end)]
    return ohlcv if not ohlcv.empty else None


LBMA_GOLD_AM_JSON_URL = "https://prices.lbma.org.uk/json/gold_am.json"


def _fetch_lbma_gold_series(divisor: float, str_start_dt: str, str_end_dt: str) -> pd.DataFrame | None:
    """Download the LBMA Gold AM fix (free public JSON feed, no API key, history from 1968-01-02).

    FRED discontinued GOLDAMGBD228NLBM in 2025 with no replacement series, so this feed
    is now the primary source for 1968~ gold history.
    """
    try:
        import requests
        resp = requests.get(LBMA_GOLD_AM_JSON_URL, timeout=30)
        resp.raise_for_status()
        records = resp.json()
    except Exception as exc:
        logger.warning("LBMA gold JSON fetch failed: %s", exc)
        return None

    rows = [(r["d"], r["v"][0]) for r in records if r.get("v") and r["v"][0] is not None]
    if not rows:
        return None

    df = pd.DataFrame(rows, columns=["date", "price"]).set_index("date")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    price = df["price"] / divisor

    out = pd.DataFrame({
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 0.0,
    })
    start = pd.Timestamp(str_start_dt).normalize()
    end = pd.Timestamp(str_end_dt).normalize()
    out = out[(out.index >= start) & (out.index < end)]
    return out if not out.empty else None


def _fetch_fred_ohlcv(series_id: str, divisor: float, str_start_dt: str, str_end_dt: str) -> pd.DataFrame | None:
    """Fetch FRED series via API or public CSV; returns OHLCV with open=high=low=close=value/divisor."""
    import io as _io
    import os

    if series_id == "GOLDAMGBD228NLBM":
        lbma_df = _fetch_lbma_gold_series(divisor, str_start_dt, str_end_dt)
        if lbma_df is not None and not lbma_df.empty:
            return lbma_df

    try:
        import requests
    except ImportError:
        return None

    api_key = os.environ.get("FRED_API_KEY", "").strip()
    raw = None

    # 1. Official FRED API with key
    if api_key:
        try:
            resp = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": api_key, "file_type": "csv",
                        "observation_start": str_start_dt, "observation_end": str_end_dt},
                timeout=30,
            )
            resp.raise_for_status()
            raw = pd.read_csv(_io.StringIO(resp.text), parse_dates=["date"], index_col="date")
            logger.info("FRED API OK (%s)", series_id)
        except Exception as exc:
            logger.warning("FRED API failed (%s): %s", series_id, exc)

    # 2. fredapi package (also uses API key)
    if raw is None and api_key:
        try:
            from fredapi import Fred as _Fred
            s = _Fred(api_key=api_key).get_series(
                series_id,
                observation_start=str_start_dt,
                observation_end=str_end_dt,
            )
            raw = s.to_frame(name="value")
            logger.info("fredapi OK (%s)", series_id)
        except Exception as exc:
            logger.warning("fredapi failed (%s): %s", series_id, exc)

    # 3. FRED public CSV (no key needed, may be blocked)
    if raw is None:
        try:
            resp = requests.get(
                f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
                timeout=30,
            )
            resp.raise_for_status()
            raw = pd.read_csv(_io.StringIO(resp.text), parse_dates=["DATE"], index_col="DATE")
            raw.index.name = None
            logger.info("FRED public CSV OK (%s)", series_id)
        except Exception as exc:
            logger.warning("FRED public CSV failed (%s): %s", series_id, exc)
            return None

    if raw is None or raw.empty:
        return None

    raw = raw[raw.iloc[:, 0] != "."].copy()
    raw.index = pd.to_datetime(raw.index)
    price = pd.to_numeric(raw.iloc[:, 0], errors="coerce").dropna() / divisor
    if price.empty:
        return None

    out = pd.DataFrame({
        "open": price, "high": price, "low": price, "close": price, "volume": 0.0,
    })
    start = pd.Timestamp(str_start_dt).normalize()
    end = pd.Timestamp(str_end_dt).normalize()
    out = out[(out.index >= start) & (out.index < end)]
    return out if not out.empty else None


def _fetch_yf_index_ohlcv(symbol: str, str_start_dt: str, str_end_dt: str) -> pd.DataFrame | None:
    """Download a Yahoo Finance index (e.g. ^SOX) and return real OHLCV (volume may be 0)."""
    try:
        import yfinance as yf
    except ImportError:
        raise ImportError("yfinance is required for index OHLCV sync") from None

    try:
        raw = yf.download(symbol, start=str_start_dt, end=str_end_dt, progress=False, auto_adjust=True)
    except Exception as exc:
        logger.warning("yfinance download(%s) failed: %s", symbol, exc)
        return None
    if raw is None or raw.empty:
        return None

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0].lower() for c in raw.columns]
    else:
        raw.columns = [c.lower() for c in raw.columns]
    raw.index = pd.to_datetime(raw.index)
    if getattr(raw.index, "tz", None) is not None:
        raw.index = raw.index.tz_localize(None)

    out = pd.DataFrame({
        "open": raw["open"],
        "high": raw["high"],
        "low": raw["low"],
        "close": raw["close"],
        "volume": raw.get("volume", 0.0),
    }).dropna(subset=["open", "high", "low", "close"])
    out["volume"] = out["volume"].fillna(0.0)
    out = out.sort_index()
    out = out[~out.index.duplicated(keep="last")]

    start = pd.Timestamp(str_start_dt).normalize()
    end = pd.Timestamp(str_end_dt).normalize()
    out = out[(out.index >= start) & (out.index < end)]
    return out if not out.empty else None


def _save_ohlcv(conn: sqlite3.Connection, ticker: str, df: pd.DataFrame) -> int:
    rows: list[tuple[Any, ...]] = []
    for idx, row in df.iterrows():
        d = pd.Timestamp(idx).normalize().strftime(DATE_FMT)
        rows.append(
            (
                ticker,
                d,
                float(row["open"]),
                float(row["high"]),
                float(row["low"]),
                float(row["close"]),
                float(row["volume"]),
            )
        )
    if not rows:
        return 0
    conn.executemany(
        f"""
        INSERT OR REPLACE INTO {TABLE}
            (ticker, date, open, high, low, close, volume)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _load_create_synthetic_fn():
    root = Path(__file__).resolve().parents[2]
    script = root / "data_pipeline" / "02_create_synthetic_leveraged_etf.py"
    if not script.is_file():
        raise FileNotFoundError(script)
    spec = importlib.util.spec_from_file_location("create_synthetic_lev", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, "create_synthetic_asymmetric_leveraged_etf", None)
    if fn is None:
        raise AttributeError("create_synthetic_asymmetric_leveraged_etf not found")
    return fn


def _refresh_synthetic_behind(
    db_path: Path,
    target_end: pd.Timestamp,
    *,
    updated_underlyings: set[str],
) -> list[dict[str, Any]]:
    create_fn = _load_create_synthetic_fn()
    results: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path)
    try:
        for underlying, synthetic, up_leverage, down_leverage in SYNTHETIC_SPECS:
            if synthetic not in _load_db_tickers(conn):
                continue
            syn_max = _ticker_max_date(conn, synthetic)
            need = underlying in updated_underlyings
            if syn_max is not None and syn_max < target_end:
                need = True
            if not need:
                continue
            try:
                create_fn(
                    underlying_ticker=underlying,
                    synthetic_ticker=synthetic,
                    up_leverage=up_leverage,
                    down_leverage=down_leverage,
                    db_path=str(db_path),
                )
                results.append({"ticker": synthetic, "status": "ok"})
            except Exception as exc:
                logger.warning("synthetic refresh %s failed: %s", synthetic, exc)
                results.append({"ticker": synthetic, "status": str(exc)})
    finally:
        conn.close()
    return results


def sync_usaetf_ohlcv_if_needed(
    db_path: str | Path | None = None,
    *,
    sleep: float = 0.15,
    as_of: pd.Timestamp | None = None,
) -> dict[str, Any]:
    """Download missing daily OHLCV for every non-synthetic DB ticker; refresh LEV* synthetics."""
    db = Path(db_path or default_project_db_path())
    target_end, gaps = plan_ticker_gaps(db, as_of=as_of)

    if not db.is_file():
        return {
            "skipped": True,
            "reason": "no_db",
            "target_end": target_end.date().isoformat(),
            "ok": 0,
            "failed": 0,
            "errors": [],
        }

    if not gaps:
        synthetic = _refresh_synthetic_behind(db, target_end, updated_underlyings=set())
        return {
            "skipped": True,
            "reason": "up_to_date",
            "target_end": target_end.date().isoformat(),
            "ok": 0,
            "failed": 0,
            "errors": [],
            "synthetic": synthetic,
        }

    gap_start_min = min(g.gap_start for g in gaps)
    end_exclusive = (target_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    str_start = gap_start_min.strftime("%Y-%m-%d")

    logger.info(
        "OHLCV sync %s -> %s for %d tickers",
        str_start,
        target_end.date().isoformat(),
        len(gaps),
    )

    errors: list[tuple[str, str]] = []
    ok = 0
    updated_underlyings: set[str] = set()
    conn = sqlite3.connect(db)

    try:
        for gap in gaps:
            t_start = gap.gap_start.strftime("%Y-%m-%d")
            try:
                if gap.ticker in _FRED_TICKERS:
                    series_id, divisor = _FRED_SERIES[gap.ticker]
                    df = _fetch_fred_ohlcv(series_id, divisor, t_start, end_exclusive)
                elif gap.ticker in _INDEX_TICKERS:
                    df = _fetch_yf_index_ohlcv(_INDEX_SERIES[gap.ticker], t_start, end_exclusive)
                else:
                    df = _fetch_fdr_usadj_ohlcv(gap.ticker, t_start, end_exclusive)
                if df is None or df.empty:
                    errors.append((gap.ticker, "empty"))
                    continue
                if gap.ticker in _FRED_TICKERS or gap.ticker in _INDEX_TICKERS:
                    prev_close = _ticker_last_close(conn, gap.ticker)
                    df = _reject_implausible_bars(gap.ticker, df, prev_close)
                    if df is None:
                        errors.append((gap.ticker, "implausible_move"))
                        continue
                n = _save_ohlcv(conn, gap.ticker, df)
                if n > 0:
                    ok += 1
                    updated_underlyings.add(gap.ticker)
                else:
                    errors.append((gap.ticker, "no_rows_saved"))
            except Exception as exc:
                errors.append((gap.ticker, str(exc)))
                logger.warning("OHLCV sync %s failed: %s", gap.ticker, exc)
            if sleep > 0:
                time.sleep(sleep)
    finally:
        conn.close()

    synthetic = _refresh_synthetic_behind(
        db, target_end, updated_underlyings=updated_underlyings
    )

    return {
        "skipped": False,
        "reason": "synced" if ok > 0 else "sync_failed",
        "gap_start": str_start,
        "target_end": target_end.date().isoformat(),
        "tickers": len(gaps),
        "ok": ok,
        "failed": len(errors),
        "errors": errors[:50],
        "synthetic": synthetic,
    }
