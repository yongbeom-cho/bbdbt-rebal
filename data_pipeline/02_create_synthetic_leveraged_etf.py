"""
Synthetic leveraged ETF generator.

Creates synthetic N-times leveraged ETF data from an underlying ETF.

Logic (example with 3x leverage on SOXX):
  - Day i OHLC is computed relative to LEV3SOXX_open[i]:
      high[i]   = lev_open[i] * (1 + L * (soxx_high[i]  / soxx_open[i] - 1))
      low[i]    = lev_open[i] * (1 + L * (soxx_low[i]   / soxx_open[i] - 1))
      close[i]  = lev_open[i] * (1 + L * (soxx_close[i] / soxx_open[i] - 1))
  - Next day's open chains from today's open:
      lev_open[i+1] = lev_open[i] * (1 + L * (soxx_open[i+1] / soxx_open[i] - 1))
  - Volume is carried over unchanged.
  - First bar's open is set to 1.0.
"""

import sqlite3
import pandas as pd
import argparse

DB_PATH = "var/data/usaetf_ohlcv_day.db"
TABLE = "usaetf_ohlcv_day"


def load_ticker(conn: sqlite3.Connection, ticker: str) -> pd.DataFrame:
    df = pd.read_sql(
        f"SELECT date, open, high, low, close, volume FROM {TABLE} WHERE ticker=? ORDER BY date",
        conn,
        params=(ticker,),
    )
    return df


def create_synthetic_leveraged_etf(
    underlying_ticker: str,
    synthetic_ticker: str,
    leverage: float,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    """
    Build a synthetic leveraged ETF from `underlying_ticker` and write it to the DB
    under `synthetic_ticker`.

    Parameters
    ----------
    underlying_ticker : str   e.g. "SOXX"
    synthetic_ticker  : str   e.g. "LEV3SOXX"
    leverage          : float e.g. 3.0
    db_path           : str   path to the SQLite DB

    Returns
    -------
    pd.DataFrame with the synthetic OHLCV rows (also written to DB).
    """
    conn = sqlite3.connect(db_path)

    base = load_ticker(conn, underlying_ticker)
    if base.empty:
        raise ValueError(f"No data found for ticker '{underlying_ticker}'")

    n = len(base)
    lev_open = [0.0] * n

    # Seed first bar
    lev_open[0] = 1.0

    # Propagate open prices forward using day-over-day open ratio × leverage
    for i in range(1, n):
        ratio = base["open"].iat[i] / base["open"].iat[i - 1]
        lev_open[i] = lev_open[i - 1] * (1.0 + leverage * (ratio - 1.0))

    lev_open_s = pd.Series(lev_open, index=base.index)

    # Compute intraday OHLC relative to each day's open
    high_ratio  = base["high"]  / base["open"]
    low_ratio   = base["low"]   / base["open"]
    close_ratio = base["close"] / base["open"]

    result = pd.DataFrame({
        "ticker": synthetic_ticker,
        "date":   base["date"],
        "open":   lev_open_s,
        "high":   lev_open_s * (1.0 + leverage * (high_ratio  - 1.0)),
        "low":    lev_open_s * (1.0 + leverage * (low_ratio   - 1.0)),
        "close":  lev_open_s * (1.0 + leverage * (close_ratio - 1.0)),
        "volume": base["volume"].values,
    })

    # Delete existing rows for the synthetic ticker, then insert
    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE} WHERE ticker=?", (synthetic_ticker,))
    result.to_sql(TABLE, conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    print(f"[OK] {synthetic_ticker}: {len(result)} bars written to '{db_path}'")
    return result


def create_synthetic_asymmetric_leveraged_etf(
    underlying_ticker: str,
    synthetic_ticker: str,
    up_leverage: float,
    down_leverage: float,
    db_path: str = DB_PATH,
) -> pd.DataFrame:
    """
    Same as create_synthetic_leveraged_etf, but applies a different leverage
    multiplier depending on the sign of each underlying return: `up_leverage`
    when the underlying moves up (ratio >= 0), `down_leverage` when it moves
    down (ratio < 0). Applied independently to the open-to-open propagation
    and to each day's own high/low/close-vs-open ratios.

    Parameters
    ----------
    underlying_ticker : str   e.g. "SOX"
    synthetic_ticker  : str   e.g. "LEV3SOX"
    up_leverage       : float e.g. 2.95
    down_leverage     : float e.g. 2.975
    db_path           : str   path to the SQLite DB

    Returns
    -------
    pd.DataFrame with the synthetic OHLCV rows (also written to DB).
    """
    conn = sqlite3.connect(db_path)

    base = load_ticker(conn, underlying_ticker)
    if base.empty:
        raise ValueError(f"No data found for ticker '{underlying_ticker}'")

    def _lev(ratio: float) -> float:
        return up_leverage if ratio >= 0 else down_leverage

    n = len(base)
    lev_open = [0.0] * n
    lev_open[0] = 1.0

    for i in range(1, n):
        ratio = base["open"].iat[i] / base["open"].iat[i - 1] - 1.0
        lev_open[i] = lev_open[i - 1] * (1.0 + _lev(ratio) * ratio)

    lev_open_s = pd.Series(lev_open, index=base.index)

    high_ratio  = base["high"]  / base["open"] - 1.0
    low_ratio   = base["low"]   / base["open"] - 1.0
    close_ratio = base["close"] / base["open"] - 1.0

    high_lev  = high_ratio.apply(_lev)
    low_lev   = low_ratio.apply(_lev)
    close_lev = close_ratio.apply(_lev)

    result = pd.DataFrame({
        "ticker": synthetic_ticker,
        "date":   base["date"],
        "open":   lev_open_s,
        "high":   lev_open_s * (1.0 + high_lev  * high_ratio),
        "low":    lev_open_s * (1.0 + low_lev   * low_ratio),
        "close":  lev_open_s * (1.0 + close_lev * close_ratio),
        "volume": base["volume"].values,
    })

    cur = conn.cursor()
    cur.execute(f"DELETE FROM {TABLE} WHERE ticker=?", (synthetic_ticker,))
    result.to_sql(TABLE, conn, if_exists="append", index=False)
    conn.commit()
    conn.close()

    print(f"[OK] {synthetic_ticker}: {len(result)} bars written to '{db_path}' "
          f"(up={up_leverage}x, down={down_leverage}x)")
    return result


def main():
    parser = argparse.ArgumentParser(description="Create synthetic leveraged ETF in DB")
    parser.add_argument("--underlying", required=True, help="Underlying ticker (e.g. SOXX)")
    parser.add_argument("--synthetic",  required=True, help="New ticker name (e.g. LEV3SOXX)")
    parser.add_argument("--leverage",   type=float, default=None, help="Symmetric leverage multiplier (default: 3)")
    parser.add_argument("--up-leverage",   type=float, default=None, help="Leverage applied on up days (requires --down-leverage)")
    parser.add_argument("--down-leverage", type=float, default=None, help="Leverage applied on down days (requires --up-leverage)")
    parser.add_argument("--db",         default=DB_PATH, help="Path to SQLite DB")
    args = parser.parse_args()

    if args.up_leverage is not None or args.down_leverage is not None:
        if args.up_leverage is None or args.down_leverage is None:
            parser.error("--up-leverage and --down-leverage must be given together")
        create_synthetic_asymmetric_leveraged_etf(
            underlying_ticker=args.underlying,
            synthetic_ticker=args.synthetic,
            up_leverage=args.up_leverage,
            down_leverage=args.down_leverage,
            db_path=args.db,
        )
    else:
        create_synthetic_leveraged_etf(
            underlying_ticker=args.underlying,
            synthetic_ticker=args.synthetic,
            leverage=args.leverage if args.leverage is not None else 3.0,
            db_path=args.db,
        )


if __name__ == "__main__":
    main()
