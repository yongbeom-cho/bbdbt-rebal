import FinanceDataReader as fdr
import time
import datetime
import os
import io
import zipfile
import requests
import pandas as pd
import pykrx as krx
import argparse
import pyupbit
import traceback
import sys
import sqlite3

# pd.options.display.max_columns = None
# pd.options.display.max_rows = None

def isNotDataframeOrEmpty(df):
    return not isinstance(df, pd.core.frame.DataFrame) or (isinstance(df, pd.core.frame.DataFrame) and df.empty)


def get_ohlcv(code: str, interval: str, count: int, to: str):
    """Get OHLCV data with retry if data count is less than requested count."""
    dfs = []
    merged = None
    max_retries = 5
    
    for retry in range(max_retries):
        df = pyupbit.get_ohlcv(code, interval=interval, count=count, to=to)
        sleep_time = 0.11 * ((retry+1)**2)
        time.sleep(sleep_time)
        if isNotDataframeOrEmpty(df):
            continue
        df = df[~df.index.duplicated(keep='last')]
        dfs.append(df)
        if retry > 0:
            print("retry : ", retry, "sleep_time : ", sleep_time, "df len : ", len(df))
        if len(dfs) == 1:
            merged = df
        elif len(dfs) > 1:
            merged = pd.concat(dfs).sort_index()

        if len(merged) == count:
            break
    
    if not dfs:
        return None
    
    # Merge all retry results
    merged = pd.concat(dfs)
    merged = merged.sort_index()
    merged = merged[~merged.index.duplicated(keep='last')]
    
    return merged


def fetch_coin_ohlcv(code: str, interval: str, str_start_dt=None, str_end_dt=None):
    dfs = []
    
    str_end_dt = str_end_dt if str_end_dt else datetime.datetime.now().strftime("%Y-%m-%d")
    to_cursor = str_end_dt
    max_iter = 100000  # safety guard
    count = 200
    for _ in range(max_iter):
        print("%s\t%s\t%d" %(code, interval, _))
        df = get_ohlcv(code, interval=interval, count=count, to=to_cursor)

        if isNotDataframeOrEmpty(df):
            print(code, interval, count, to_cursor)
            break
        dfs.append(df)

        str_earliest_date = df.index.min().strftime("%Y-%m-%d %H:%M:%S")

        if str_start_dt and str_earliest_date <= str_start_dt:
            break
        to_cursor = (
            pd.to_datetime(str_earliest_date)
            .tz_localize("Asia/Seoul")
            .tz_convert("UTC")
        ).strftime("%Y-%m-%d %H:%M:%S")

        time.sleep(0.11)  # throttle to avoid rate-limit

    if not dfs:
        return None

    merged = pd.concat(dfs)
    if str_start_dt and str_end_dt:
        start_dt = datetime.datetime.strptime(str_start_dt, "%Y-%m-%d") \
                        .replace(hour=9, minute=0, second=0, microsecond=0)
        end_dt = datetime.datetime.strptime(str_end_dt, "%Y-%m-%d") \
                        .replace(hour=9, minute=0, second=0, microsecond=0)
        merged = merged[(merged.index >= start_dt) & (merged.index < end_dt)]
    merged = merged.sort_index()
    merged = merged[~merged.index.duplicated(keep='last')]
    
    return merged

def clean_and_save_to_db(df: pd.DataFrame, conn: sqlite3.Connection, table_name: str, ticker: str, interval: str):
    """Persist OHLCV rows: same as coin / coin-binance (table DDL, date key %Y%m%d%H%M, columns).

    df must use a DatetimeIndex and columns open, high, low, close, volume (float-like).
    """
    print("clean_and_save_to_db", ticker, interval)
    if 'value' in df.columns:
        df = df.drop('value', axis=1)
    
    df['ticker'] = ticker
    df['date'] = df.index
    df['date'] = df['date'].apply(lambda d: d.tz_localize(None) if getattr(d, "tzinfo", None) else d)

    # date formatting depending on interval granularity
    
    fmt = "%Y%m%d%H%M"
    df['date'] = df['date'].apply(lambda d: d.strftime(fmt))

    df = df[['ticker', 'date', 'open', 'high', 'low', 'close', 'volume']].reset_index(drop=True)
    
    # Use INSERT OR REPLACE for upsert
    cursor = conn.cursor()
    for _, row in df.iterrows():
        cursor.execute(f"""
            INSERT OR REPLACE INTO {table_name} (ticker, date, open, high, low, close, volume)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row['ticker'], row['date'], row['open'], row['high'], row['low'], row['close'], row['volume']))
    conn.commit()


def create_table_if_not_exists(conn: sqlite3.Connection, table_name: str):
    """Create OHLCV table if it doesn't exist."""
    cursor = conn.cursor()
    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {table_name} (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL NOT NULL,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.commit()


COIN_INTERVALS = ["day", "minute1", "minute3", "minute5", "minute10", "minute15", "minute30", "minute60", "minute240", "week", "month"]
BINANCE_INTERVALS = [
    "1m",   # 1분
    "3m",   # 3분
    "5m",   # 5분
    "15m",  # 15분
    "30m",  # 30분
    "1h",   # 1시간
    "2h",   # 2시간
    "4h",   # 4시간
    "6h",   # 6시간
    "8h",   # 8시간
    "12h",  # 12시간
    "1d",   # 1일
    "3d",   # 3일
    "1w",   # 1주
    "1M"    # 1개월
]

BINANCE_VISION_BASE = "https://data.binance.vision/data/spot"
BINANCE_API_BASE    = "https://api.binance.com/api/v3/klines"

BINANCE_TIMEFRAME_MS = {
    "1m":  60_000,
    "3m":  180_000,
    "5m":  300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h":  3_600_000,
    "2h":  7_200_000,
    "4h":  14_400_000,
    "6h":  21_600_000,
    "8h":  28_800_000,
    "12h": 43_200_000,
    "1d":  86_400_000,
    "3d":  259_200_000,
    "1w":  604_800_000,
    "1M":  2_592_000_000,
}

BINANCE_TO_COIN_INTERVAL = {
    "1m":  "minute1",
    "3m":  "minute3",
    "5m":  "minute5",
    "15m": "minute15",
    "30m": "minute30",
    "1h":  "minute60",
    "2h":  "minute120",
    "4h":  "minute240",
    "6h":  "minute360",
    "8h":  "minute480",
    "12h": "minute720",
    "1d":  "day",
    "3d":  "day3",
    "1w":  "week",
    "1M":  "month",
}


def get_binance_usdt_tickers() -> list[str]:
    """Return all active USDT spot trading pairs via Binance REST API."""
    resp = requests.get("https://api.binance.com/api/v3/exchangeInfo", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return sorted([
        s['symbol'] for s in data['symbols']
        if s['quoteAsset'] == 'USDT'
        and s['status'] == 'TRADING'
        and s['isSpotTradingAllowed']
    ])


def _parse_kline_zip(content: bytes) -> pd.DataFrame | None:
    """Parse a Binance Vision ZIP file into an OHLCV DataFrame.

    Handles files where open_time is stored in nanoseconds, milliseconds, or seconds.
    """
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        with zf.open(zf.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, usecols=[0, 1, 2, 3, 4, 5])
    df.columns = ['open_time', 'open', 'high', 'low', 'close', 'volume']
    df['open_time'] = pd.to_numeric(df['open_time'], errors='coerce')
    df = df.dropna(subset=['open_time'])
    if df.empty:
        return None

    # Auto-detect timestamp unit by order of magnitude and normalise to ms
    # ns: ~1.7e18 (2024), ms: ~1.7e12 (2024), s: ~1.7e9 (2024)
    sample = df['open_time'].iloc[0]
    if sample > 1e16:          # nanoseconds → ms
        df['open_time'] = df['open_time'] / 1_000_000
    elif sample < 1e11:        # seconds → ms
        df['open_time'] = df['open_time'] * 1_000

    # Filter to valid ms range (year 2000–2100)
    _MS_MIN = 946_684_800_000
    _MS_MAX = 4_102_444_800_000
    df = df[(df['open_time'] >= _MS_MIN) & (df['open_time'] <= _MS_MAX)]
    if df.empty:
        return None

    # Convert ms → s before calling pd.to_datetime to avoid OutOfBoundsDatetime
    # in older pandas versions that mishandle large ms integers
    df['date'] = pd.to_datetime(df['open_time'] / 1000, unit='s')
    df = df.set_index('date')[['open', 'high', 'low', 'close', 'volume']].astype(float)
    return df


def fetch_binance_api_ohlcv(symbol: str, timeframe: str, since_ms: int, until_ms: int) -> pd.DataFrame | None:
    """Fetch klines from Binance REST API for a given ms timestamp range."""
    tf_ms = BINANCE_TIMEFRAME_MS[timeframe]
    all_rows = []
    cur = since_ms
    while cur < until_ms:
        try:
            resp = requests.get(
                BINANCE_API_BASE,
                params={"symbol": symbol, "interval": timeframe,
                        "startTime": cur, "limit": 1000},
                timeout=30,
            )
            if resp.status_code != 200:
                break
            rows = resp.json()
            if not rows:
                break
            all_rows.extend(rows)
            last_open = rows[-1][0]
            cur = last_open + tf_ms
            if len(rows) < 1000:
                break
            time.sleep(0.1)
        except Exception:
            print(traceback.format_exc())
            break

    if not all_rows:
        return None

    df = pd.DataFrame(all_rows, columns=[
        'open_time', 'open', 'high', 'low', 'close', 'volume',
        'close_time', 'qv', 'trades', 'tbbv', 'tbqv', 'ignore'
    ])
    df['date'] = pd.to_datetime(df['open_time'].astype('int64'), unit='ms')
    df = df.set_index('date')[['open', 'high', 'low', 'close', 'volume']].astype(float)
    df = df[df.index < pd.Timestamp(until_ms, unit='ms')]
    return df if not df.empty else None


def _download_vision_file(symbol: str, timeframe: str, year: int, month: int, day: int = None) -> pd.DataFrame | None:
    """Download a monthly or daily kline ZIP from Binance Vision and parse it."""
    if day is not None:
        fname = f"{symbol}-{timeframe}-{year}-{month:02d}-{day:02d}.zip"
        url = f"{BINANCE_VISION_BASE}/daily/klines/{symbol}/{timeframe}/{fname}"
    else:
        fname = f"{symbol}-{timeframe}-{year}-{month:02d}.zip"
        url = f"{BINANCE_VISION_BASE}/monthly/klines/{symbol}/{timeframe}/{fname}"

    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 404:
                return None
            if resp.status_code == 200:
                return _parse_kline_zip(resp.content)
            time.sleep(1.0 * (attempt + 1))
        except Exception:
            if attempt == 2:
                print(traceback.format_exc())
            time.sleep(1.0 * (attempt + 1))
    return None


def fetch_binance_vision_ohlcv(symbol: str, timeframe: str,
                                str_start_dt=None, str_end_dt=None) -> pd.DataFrame | None:
    """Fetch OHLCV data: Binance Vision dumps first, REST API for uncovered recent data."""
    now = datetime.datetime.utcnow()
    start = datetime.datetime.strptime(str_start_dt, "%Y-%m-%d") if str_start_dt else datetime.datetime(2017, 1, 1)
    end   = datetime.datetime.strptime(str_end_dt,   "%Y-%m-%d") if str_end_dt   else now

    first_of_current_month = datetime.datetime(now.year, now.month, 1)
    dfs = []
    consecutive_none = 0

    # Monthly dumps for completed months; stop early if Vision has a sustained gap
    cur = datetime.datetime(start.year, start.month, 1)
    monthly_end = min(end, first_of_current_month)
    while cur < monthly_end:
        print(f"  monthly {symbol} {timeframe} {cur.year}-{cur.month:02d}")
        df = _download_vision_file(symbol, timeframe, cur.year, cur.month)
        if df is not None and not df.empty:
            dfs.append(df)
            consecutive_none = 0
        else:
            consecutive_none += 1
            # Vision typically has ~1 year upload lag; 12 consecutive Nones = switch to REST API
            if consecutive_none >= 12:
                print(f"  Vision gap detected at {cur.year}-{cur.month:02d}, switching to REST API")
                break
        cur = (cur + datetime.timedelta(days=32)).replace(day=1)
        time.sleep(0.05)

    # Daily dumps for current month (only if Vision is still providing recent data)
    if consecutive_none < 12 and end > first_of_current_month:
        day_cursor = max(start, first_of_current_month)
        day_limit  = min(end, now.replace(hour=0, minute=0, second=0, microsecond=0))
        while day_cursor < day_limit:
            print(f"  daily   {symbol} {timeframe} {day_cursor.date()}")
            df = _download_vision_file(symbol, timeframe, day_cursor.year, day_cursor.month, day_cursor.day)
            if df is not None and not df.empty:
                dfs.append(df)
            day_cursor += datetime.timedelta(days=1)
            time.sleep(0.05)

    # REST API fallback: fill the gap from last Vision date to end
    vision_last_dt = pd.concat(dfs).index.max() if dfs else None
    api_since_dt = (vision_last_dt + datetime.timedelta(milliseconds=BINANCE_TIMEFRAME_MS[timeframe])) \
                   if vision_last_dt is not None else start

    if api_since_dt < end:
        since_ms = int(api_since_dt.timestamp() * 1000)
        until_ms = int(end.timestamp() * 1000)
        print(f"  REST API {symbol} {timeframe} from {api_since_dt.date()} to {end.date()}")
        df_api = fetch_binance_api_ohlcv(symbol, timeframe, since_ms, until_ms)
        if df_api is not None and not df_api.empty:
            dfs.append(df_api)

    if not dfs:
        return None

    merged = pd.concat(dfs).sort_index()
    merged = merged[~merged.index.duplicated(keep='last')]
    merged = merged[(merged.index >= start) & (merged.index < end)]
    return merged if not merged.empty else None


# US ETF / USA stocks: daily only (FinanceDataReader), same adj-close scaling as xgb_trader 01_get_ohlcv_etf.py
# ETF_TICKERS_DEFAULT = [
#     "TQQQ", "SOXL", "FNGU", "UPRO", "UDOW", "TECL", "CURE", "NAIL", "FAS",
#     "GLD", "QQQ", "SPY", "DGT", "IYF", "SOXX"
# ]
ETF_TICKERS_DEFAULT = [
    "QLD", "USD", "SSO"
]

# FRED series: project ticker name → (series_id, price_divisor)
# "GOLD" represents London Gold AM fix / 10 ≈ synthetic GLD price (back to 1968-04-01)
FRED_TICKER_SPECS: dict[str, tuple[str, float]] = {
    "GOLD": ("GOLDAMGBD228NLBM", 10.0),
}

# swing_trader/sbin/get_stock_list_usa.py 와 동일: FDR StockListing 으로 미국 시장 유니버스
USA_FDR_LISTING_MARKETS = ["NASDAQ", "S&P500", "NYSE", "AMEX"]


def get_usa_tickers_via_fdr_stock_listing() -> list[str]:
    """NASDAQ / S&P500 / NYSE / AMEX 리스팅을 합쳐 Symbol 목록 (중복은 마지막 Name 으로 덮어씀)."""
    usa_code_names: dict[str, str] = {}
    for usa_market in USA_FDR_LISTING_MARKETS:
        try:
            listing = fdr.StockListing(usa_market)
        except Exception:
            print(f"StockListing({usa_market!r}) failed:")
            print(traceback.format_exc())
            continue
        if isNotDataframeOrEmpty(listing) or "Symbol" not in listing.columns:
            print(f"StockListing({usa_market!r}): no Symbol column or empty")
            continue
        for _, row in listing.iterrows():
            sym = row["Symbol"]
            if pd.isna(sym):
                continue
            sym = str(sym).strip()
            if not sym:
                continue
            name = row["Name"] if "Name" in listing.columns and not pd.isna(row.get("Name")) else ""
            usa_code_names[sym] = str(name)
        time.sleep(0.15)
    return sorted(usa_code_names.keys())


def _fdr_df_to_ohlcv(df: pd.DataFrame) -> pd.DataFrame | None:
    """Normalize FDR columns to OHLCV float frame with DatetimeIndex (naive)."""
    if isNotDataframeOrEmpty(df):
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


def fetch_fdr_usadj_ohlcv(
    code: str, str_start_dt=None, str_end_dt=None
) -> pd.DataFrame | None:
    """Daily OHLCV from FinanceDataReader; OHLC scaled by adj_close/close (usaetf / usastock pipeline)."""
    try:
        raw = fdr.DataReader(code)
    except Exception:
        print(traceback.format_exc())
        return None

    ohlcv = _fdr_df_to_ohlcv(raw)
    if ohlcv is None or ohlcv.empty:
        return None

    start = (
        datetime.datetime.strptime(str_start_dt, "%Y-%m-%d")
        if str_start_dt
        else datetime.datetime(1990, 1, 1)
    )
    end = (
        datetime.datetime.strptime(str_end_dt, "%Y-%m-%d")
        if str_end_dt
        else datetime.datetime.utcnow()
    )
    ohlcv = ohlcv[(ohlcv.index >= start) & (ohlcv.index < end)]
    return ohlcv if not ohlcv.empty else None


LBMA_GOLD_AM_JSON_URL = "https://prices.lbma.org.uk/json/gold_am.json"


def fetch_lbma_gold_ohlcv(divisor: float, str_start_dt=None, str_end_dt=None) -> pd.DataFrame | None:
    """Download the LBMA Gold AM fix (free public JSON feed, no API key, history from 1968-01-02).

    FRED discontinued GOLDAMGBD228NLBM in 2025 with no replacement series, so this feed
    is now the primary source for 1968~ gold history.
    """
    try:
        resp = requests.get(LBMA_GOLD_AM_JSON_URL, timeout=30)
        resp.raise_for_status()
        records = resp.json()
    except Exception:
        print(traceback.format_exc())
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
    if str_start_dt:
        out = out[out.index >= pd.Timestamp(str_start_dt)]
    if str_end_dt:
        out = out[out.index < pd.Timestamp(str_end_dt)]
    return out if not out.empty else None


def fetch_fred_ohlcv(series_id: str, divisor: float, str_start_dt=None, str_end_dt=None) -> pd.DataFrame | None:
    """Download a FRED series and return OHLCV (open=high=low=close=value/divisor, volume=0).

    Priority:
    0. LBMA free JSON feed  → gold only (GOLDAMGBD228NLBM); FRED discontinued this series
       in 2025, so this is the primary source now (1968-01-02~)
    1. FRED_API_KEY env var → official FRED API (for any other still-active FRED series)
    2. FRED public CSV URL  → works on most networks (no API key needed)
    3. GC=F via yfinance   → last-resort fallback (data from 2000-08-30 only)
    """
    import io as _io
    import os

    if series_id == "GOLDAMGBD228NLBM":
        lbma_df = fetch_lbma_gold_ohlcv(divisor, str_start_dt, str_end_dt)
        if lbma_df is not None and not lbma_df.empty:
            print(f"  LBMA JSON OK ({series_id})")
            return lbma_df

    api_key = os.environ.get("FRED_API_KEY", "").strip()

    raw_df = None

    # --- 1. Official FRED API ---
    if api_key:
        params = {
            "series_id": series_id,
            "api_key": api_key,
            "file_type": "csv",
        }
        if str_start_dt:
            params["observation_start"] = str_start_dt
        if str_end_dt:
            params["observation_end"] = str_end_dt
        url = "https://api.stlouisfed.org/fred/series/observations"
        try:
            resp = requests.get(url, params=params, timeout=30)
            resp.raise_for_status()
            df = pd.read_csv(_io.StringIO(resp.text), parse_dates=["date"], index_col="date")
            raw_df = df[["value"]].copy()
            print(f"  FRED API OK ({series_id})")
        except Exception:
            print(traceback.format_exc())

    # --- 2. fredapi package (also uses API key) ---
    if raw_df is None and api_key:
        try:
            from fredapi import Fred as _Fred
            _fred = _Fred(api_key=api_key)
            s = _fred.get_series(series_id,
                                 observation_start=str_start_dt or "1900-01-01",
                                 observation_end=str_end_dt or datetime.datetime.now().strftime("%Y-%m-%d"))
            raw_df = s.to_frame(name="value")
            print(f"  fredapi OK ({series_id})")
        except Exception:
            print(f"  fredapi failed ({series_id}): {traceback.format_exc().splitlines()[-1]}")

    # --- 3. FRED public CSV ---
    if raw_df is None:
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            raw_df = pd.read_csv(_io.StringIO(resp.text), parse_dates=["DATE"], index_col="DATE")
            raw_df.index.name = "date"
            print(f"  FRED public CSV OK ({series_id})")
        except Exception:
            print(f"  FRED public CSV failed ({series_id}): {traceback.format_exc().splitlines()[-1]}")

    # --- 3. GC=F fallback (Yahoo Finance, 2000-08-30~) ---
    if raw_df is None:
        print(f"  Falling back to GC=F (Yahoo Finance) — data available from 2000-08-30 only")
        print(f"  To get 1968~ data: set FRED_API_KEY env var (free: https://fred.stlouisfed.org/docs/api/api_key.html)")
        try:
            import yfinance as yf
            start_yf = str_start_dt or "1968-01-01"
            end_yf = str_end_dt or datetime.datetime.now().strftime("%Y-%m-%d")
            gc = yf.download("GC=F", start=start_yf, end=end_yf, progress=False, auto_adjust=True)
            if gc.empty:
                return None
            if isinstance(gc.columns, pd.MultiIndex):
                gc.columns = [c[0].lower() for c in gc.columns]
            else:
                gc.columns = [c.lower() for c in gc.columns]
            gc.index = pd.to_datetime(gc.index)
            if getattr(gc.index, "tz", None):
                gc.index = gc.index.tz_localize(None)
            out = pd.DataFrame({
                "open":   gc["open"]  / divisor,
                "high":   gc["high"]  / divisor,
                "low":    gc["low"]   / divisor,
                "close":  gc["close"] / divisor,
                "volume": gc.get("volume", 0.0),
            }).dropna(subset=["open", "close"])
            if str_start_dt:
                out = out[out.index >= pd.Timestamp(str_start_dt)]
            if str_end_dt:
                out = out[out.index < pd.Timestamp(str_end_dt)]
            return out if not out.empty else None
        except Exception:
            print(traceback.format_exc())
            return None

    # --- convert FRED single-price column to OHLCV ---
    raw_df = raw_df[raw_df.iloc[:, 0] != "."].copy()
    raw_df.index = pd.to_datetime(raw_df.index)
    price = pd.to_numeric(raw_df.iloc[:, 0], errors="coerce").dropna() / divisor
    price.index.name = None

    out = pd.DataFrame({
        "open": price,
        "high": price,
        "low": price,
        "close": price,
        "volume": 0.0,
    })
    if str_start_dt:
        out = out[out.index >= pd.Timestamp(str_start_dt)]
    if str_end_dt:
        out = out[out.index < pd.Timestamp(str_end_dt)]
    return out if not out.empty else None


def _clip_ohlcv_datetime_window(
    df: pd.DataFrame, str_start_dt=None, str_end_dt=None
) -> pd.DataFrame | None:
    """Filter daily OHLCV index to [start, end) in local date (usaetf / usastock / korstock)."""
    if df is None or df.empty:
        return None
    start = (
        datetime.datetime.strptime(str_start_dt, "%Y-%m-%d")
        if str_start_dt
        else datetime.datetime(1990, 1, 1)
    )
    end = (
        datetime.datetime.strptime(str_end_dt, "%Y-%m-%d")
        if str_end_dt
        else datetime.datetime.utcnow()
    )
    out = df[(df.index >= start) & (df.index < end)]
    return out if not out.empty else None


def _pykrx_df_to_ohlcv(raw: pd.DataFrame) -> pd.DataFrame | None:
    """pykrx 일봉 → open/high/low/close/volume, DatetimeIndex (get_ohlcv.py 와 동일 컬럼)."""
    if isNotDataframeOrEmpty(raw):
        return None
    need = ["시가", "고가", "저가", "종가", "거래량"]
    if not all(c in raw.columns for c in need):
        return None
    df = raw[need].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = df["volume"].fillna(0.0)
    df = df.dropna(subset=["open", "high", "low", "close"])
    if df.empty:
        return None
    df.index = pd.to_datetime(raw.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def get_kor_listed_code_names() -> list[tuple[str, str]]:
    """get_stock_list.py: KRX 상장, KOSPI/KOSDAQ 만, 이름에 '스팩' 제외."""
    try:
        krx_df = fdr.StockListing("KRX")
    except Exception:
        print("StockListing('KRX') failed:")
        print(traceback.format_exc())
        return []
    if isNotDataframeOrEmpty(krx_df) or "Market" not in krx_df.columns:
        return []
    krx_df = krx_df.loc[(krx_df.Market == "KOSPI") | (krx_df.Market == "KOSDAQ")].reset_index(
        drop=True
    )
    out: list[tuple[str, str]] = []
    for _, row in krx_df.iterrows():
        code = row.get("Code")
        if pd.isna(code):
            continue
        code = str(code).strip().zfill(6)
        if not code or code == "000000":
            continue
        name = row.get("Name")
        name = "" if pd.isna(name) else str(name)
        if "스팩" in name:
            continue
        out.append((code, name))
    return out


def get_kor_delisted_entries() -> list[tuple[str, str, str, str]]:
    """get_stock_list.py: KRX-DELISTING, KOSPI/KOSDAQ. (code, name, listing_ymd, delisting_ymd)."""
    try:
        d = fdr.StockListing("KRX-DELISTING")
    except Exception:
        print("StockListing('KRX-DELISTING') failed:")
        print(traceback.format_exc())
        return []
    if isNotDataframeOrEmpty(d) or "Market" not in d.columns:
        return []
    need_cols = {"Symbol", "ListingDate", "DelistingDate"}
    if not need_cols.issubset(set(d.columns)):
        return []
    d = d.loc[(d.Market == "KOSPI") | (d.Market == "KOSDAQ")].reset_index(drop=True)
    out: list[tuple[str, str, str, str]] = []
    for _, row in d.iterrows():
        sym = row.get("Symbol")
        if pd.isna(sym):
            continue
        code = str(sym).strip().zfill(6)
        if not code or code == "000000":
            continue
        name = row.get("Name")
        name = "" if pd.isna(name) else str(name)
        if "스팩" in name:
            continue
        try:
            list_start = pd.Timestamp(row["ListingDate"]).strftime("%Y%m%d")
            list_end = pd.Timestamp(row["DelistingDate"]).strftime("%Y%m%d")
        except Exception:
            continue
        out.append((code, name, list_start, list_end))
    return out


def fetch_kor_listed_ohlcv(
    code: str, str_start_dt=None, str_end_dt=None
) -> pd.DataFrame | None:
    """get_ohlcv.py: krx.stock.get_market_ohlcv('19010101', last_date, code)."""
    last_date = datetime.datetime.now().strftime("%Y%m%d")
    try:
        raw = krx.stock.get_market_ohlcv("19010101", last_date, code)
    except Exception:
        print(traceback.format_exc())
        return None
    df = _pykrx_df_to_ohlcv(raw)
    return _clip_ohlcv_datetime_window(df, str_start_dt, str_end_dt)


def fetch_kor_delisted_ohlcv(
    code: str, list_start_ymd: str, list_end_ymd: str, str_start_dt=None, str_end_dt=None
) -> pd.DataFrame | None:
    """get_ohlcv.py: get_market_ohlcv_by_date(start, last, code)."""
    try:
        raw = krx.stock.get_market_ohlcv_by_date(list_start_ymd, list_end_ymd, code)
    except Exception:
        print(traceback.format_exc())
        return None
    df = _pykrx_df_to_ohlcv(raw)
    return _clip_ohlcv_datetime_window(df, str_start_dt, str_end_dt)


#--market : [coin, coin-binance, usaetf, usastock, korstock, ...]
#--interval (coin)    : [day, minute1, minute3, minute5, minute10, minute15, minute30, minute60, minute240, week, month]
#--interval (binance) : [1h, 4h, 1d, 1w]
#--interval (usaetf, usastock, korstock): [day] only (daily)
#--date : {YYYYMMDD or "all"}
#--output_dir : {output_dir for db file}
parser = argparse.ArgumentParser(description='get_daily_ohlcv_data')
parser.add_argument('--root_dir', type=str, default="/Users/yongbeom/cyb/project/2025/quant_devel")
parser.add_argument('--date', type=str, required=True, help="YYYYMMDD or all")
parser.add_argument('--market', type=str, default="coin")
parser.add_argument('--interval', type=str, default="minute1")
parser.add_argument('--output_dir', type=str, default="var/data")
args = parser.parse_args()

# Create output directory for db file
output_dir = os.path.join(args.root_dir, args.output_dir)
os.makedirs(output_dir, exist_ok=True)

if args.market == 'coin' and args.interval not in COIN_INTERVALS:
    print(f"Unsupported interval for coin: {args.interval}. Use one of {COIN_INTERVALS}")
    sys.exit(1)

if args.market == 'coin-binance':
    # Accept both binance-style (4h) and coin-style (minute240) intervals
    COIN_TO_BINANCE_INTERVAL = {v: k for k, v in BINANCE_TO_COIN_INTERVAL.items()}
    if args.interval in BINANCE_INTERVALS:
        interval = args.interval
        normalized_interval = BINANCE_TO_COIN_INTERVAL[interval]
    elif args.interval in COIN_TO_BINANCE_INTERVAL:
        normalized_interval = args.interval
        interval = COIN_TO_BINANCE_INTERVAL[normalized_interval]
    else:
        print(f"Unsupported interval for coin-binance: {args.interval}")
        print(f"  Binance style: {BINANCE_INTERVALS}")
        print(f"  Coin style:    {list(COIN_TO_BINANCE_INTERVAL.keys())}")
        sys.exit(1)
    db_market = 'coin'
elif args.market in ('usaetf', 'usastock', 'korstock', 'fred'):
    if args.interval != 'day':
        print(f"market {args.market} only supports daily data; use --interval day (got {args.interval})")
        sys.exit(1)
    interval = 'day'
    normalized_interval = 'day'
    db_market = 'usaetf' if args.market == 'fred' else args.market
else:
    interval = args.interval
    normalized_interval = interval
    db_market = args.market

# DB file path: {output_dir}/{db_market}_ohlcv_{normalized_interval}.db
db_path = os.path.join(output_dir, f"{db_market}_ohlcv_{normalized_interval}.db")
conn = sqlite3.connect(db_path)

str_start_dt = None
str_end_dt = None
if args.date.lower() == "all":
    str_end_dt = datetime.datetime.now().strftime("%Y-%m-%d")
else:
    # args.date에 어제의 날짜가 들어옴.
    date = datetime.datetime.strptime(args.date, "%Y%m%d")
    str_start_dt = datetime.datetime(date.year, date.month, date.day).strftime("%Y-%m-%d")
    str_end_dt = (date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")


if args.market == 'coin':
    tickers = [t for t in pyupbit.get_tickers() if 'KRW-' in t]
    print("tickers len :", len(tickers))

    table_name = f"{args.market}_ohlcv_{args.interval}"
    create_table_if_not_exists(conn, table_name)
    print(f"Using table: {table_name} in DB: {db_path}")

    for ticker_i, ticker in enumerate(tickers):
        print(f"start {ticker_i+1}. {ticker}")
        try:
            df = fetch_coin_ohlcv(ticker, interval, str_start_dt, str_end_dt)
            if isNotDataframeOrEmpty(df):
                print(f"{ticker} - no data")
                continue

            clean_and_save_to_db(df, conn, table_name, ticker, interval)
            print(f"{ticker} - saved {len(df)} records")
        except Exception:
            print(f"error on {ticker}")
            print(traceback.format_exc())
            time.sleep(1.0)

    conn.close()
    print(f"All data saved to {db_path}")

elif args.market == 'coin-binance':
    tickers = get_binance_usdt_tickers()
    print("tickers len :", len(tickers))

    table_name = f"coin_ohlcv_{normalized_interval}"
    create_table_if_not_exists(conn, table_name)
    print(f"Using table: {table_name} in DB: {db_path}")

    for ticker_i, ticker in enumerate(tickers):
        print(f"start {ticker_i+1}/{len(tickers)}. {ticker}")
        try:
            df = fetch_binance_vision_ohlcv(ticker, interval, str_start_dt, str_end_dt)
            if df is None or df.empty:
                print(f"{ticker} - no data")
                continue
            # Binance Vision uses "BTCUSDT" → store as "BTC-USDT"
            save_ticker = "USDT-" + ticker[:-4]
            clean_and_save_to_db(df, conn, table_name, save_ticker, normalized_interval)
            print(f"{ticker} - saved {len(df)} records")
        except Exception:
            print(f"error on {ticker}")
            print(traceback.format_exc())
            time.sleep(1.0)

    conn.close()
    print(f"All data saved to {db_path}")

elif args.market == 'usaetf':
    tickers = ETF_TICKERS_DEFAULT
    print("tickers len :", len(tickers), tickers)

    table_name = f"{args.market}_ohlcv_{normalized_interval}"
    create_table_if_not_exists(conn, table_name)
    print(f"Using table: {table_name} in DB: {db_path}")

    for ticker_i, ticker in enumerate(tickers):
        print(f"start {ticker_i+1}/{len(tickers)}. {ticker}")
        try:
            df = fetch_fdr_usadj_ohlcv(ticker, str_start_dt, str_end_dt)
            if isNotDataframeOrEmpty(df):
                print(f"{ticker} - no data")
                continue
            clean_and_save_to_db(df, conn, table_name, ticker, normalized_interval)
            print(f"{ticker} - saved {len(df)} records")
        except Exception:
            print(f"error on {ticker}")
            print(traceback.format_exc())
            time.sleep(1.0)

    conn.close()
    print(f"All data saved to {db_path}")

elif args.market == 'usastock':
    print("fetching USA ticker universe (FinanceDataReader StockListing)...")
    tickers = get_usa_tickers_via_fdr_stock_listing()
    if not tickers:
        print("No USA tickers from StockListing; exiting.")
        conn.close()
        sys.exit(1)
    print("tickers len :", len(tickers))

    table_name = f"{args.market}_ohlcv_{normalized_interval}"
    create_table_if_not_exists(conn, table_name)
    print(f"Using table: {table_name} in DB: {db_path}")

    for ticker_i, ticker in enumerate(tickers):
        print(f"start {ticker_i+1}/{len(tickers)}. {ticker}")
        try:
            df = fetch_fdr_usadj_ohlcv(ticker, str_start_dt, str_end_dt)
            if isNotDataframeOrEmpty(df):
                print(f"{ticker} - no data")
                continue
            clean_and_save_to_db(df, conn, table_name, ticker, normalized_interval)
            print(f"{ticker} - saved {len(df)} records")
        except Exception:
            print(f"error on {ticker}")
            print(traceback.format_exc())
            time.sleep(1.0)

    conn.close()
    print(f"All data saved to {db_path}")

elif args.market == 'fred':
    print("tickers:", list(FRED_TICKER_SPECS.keys()))
    table_name = "usaetf_ohlcv_day"
    create_table_if_not_exists(conn, table_name)
    print(f"Using table: {table_name} in DB: {db_path}")

    for ticker_name, (series_id, divisor) in FRED_TICKER_SPECS.items():
        print(f"fetching {ticker_name} ({series_id})")
        try:
            df = fetch_fred_ohlcv(series_id, divisor, str_start_dt, str_end_dt)
            if isNotDataframeOrEmpty(df):
                print(f"{ticker_name} - no data")
                continue
            clean_and_save_to_db(df, conn, table_name, ticker_name, normalized_interval)
            print(f"{ticker_name} - saved {len(df)} records")
        except Exception:
            print(f"error on {ticker_name}")
            print(traceback.format_exc())

    conn.close()
    print(f"All data saved to {db_path}")

elif args.market == 'korstock':
    print("fetching KRX listed + KRX-DELISTING (FinanceDataReader StockListing)...")
    listed = get_kor_listed_code_names()
    delisted = get_kor_delisted_entries()
    n_total = len(listed) + len(delisted)
    if n_total == 0:
        print("No Korean stock tickers from StockListing; exiting.")
        conn.close()
        sys.exit(1)
    print(f"listed: {len(listed)}, delisted: {len(delisted)}, total jobs: {n_total}")

    table_name = f"{args.market}_ohlcv_{normalized_interval}"
    create_table_if_not_exists(conn, table_name)
    print(f"Using table: {table_name} in DB: {db_path}")

    n_done = 0
    for code, name in listed:
        n_done += 1
        print(f"start {n_done}/{n_total} listed {code} {name}")
        try:
            df = fetch_kor_listed_ohlcv(code, str_start_dt, str_end_dt)
            if isNotDataframeOrEmpty(df):
                print(f"{code} - no data")
                time.sleep(0.1)
                continue
            clean_and_save_to_db(df, conn, table_name, code, normalized_interval)
            print(f"{code} - saved {len(df)} records")
        except Exception:
            print(f"error on {code}")
            print(traceback.format_exc())
            time.sleep(1.0)
        time.sleep(0.1)

    for code, name, list_start, list_end in delisted:
        n_done += 1
        print(f"start {n_done}/{n_total} delisted {code} {name} ({list_start}-{list_end})")
        try:
            df = fetch_kor_delisted_ohlcv(
                code, list_start, list_end, str_start_dt, str_end_dt
            )
            if isNotDataframeOrEmpty(df):
                print(f"{code} - no data")
                time.sleep(0.1)
                continue
            clean_and_save_to_db(df, conn, table_name, code, normalized_interval)
            print(f"{code} - saved {len(df)} records")
        except Exception:
            print(f"error on {code}")
            print(traceback.format_exc())
            time.sleep(1.0)
        time.sleep(0.1)

    conn.close()
    print(f"All data saved to {db_path}")