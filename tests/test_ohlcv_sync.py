"""OHLCV gap planning for viewer sync."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from bear_bull_drop_buy.ohlcv_sync import (  # noqa: E402
    SYNTHETIC_SPECS,
    plan_ticker_gaps,
    sync_target_end,
)


@pytest.fixture
def mini_db(tmp_path: Path) -> Path:
    db = tmp_path / "usaetf_ohlcv_day.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE usaetf_ohlcv_day (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (ticker, date)
        )
        """
    )
    conn.execute(
        "INSERT INTO usaetf_ohlcv_day VALUES (?,?,?,?,?,?,?)",
        ("SOXL", "202604100000", 1.0, 1.1, 0.9, 1.05, 100.0),
    )
    conn.execute(
        "INSERT INTO usaetf_ohlcv_day VALUES (?,?,?,?,?,?,?)",
        ("LEV3QQQ", "202604100000", 1.0, 1.1, 0.9, 1.05, 100.0),
    )
    conn.commit()
    conn.close()
    return db


def test_sync_target_end_excludes_today():
    # Mon 2026-06-01 → last completed US session is Fri 2026-05-29
    assert sync_target_end(pd.Timestamp("2026-06-01")) == pd.Timestamp("2026-05-29")


def test_plan_skips_synthetic_tickers(mini_db: Path):
    target, gaps = plan_ticker_gaps(mini_db, as_of=pd.Timestamp("2026-06-01"))
    assert target == pd.Timestamp("2026-05-29")
    tickers = {g.ticker for g in gaps}
    assert "SOXL" in tickers
    assert "LEV3QQQ" not in tickers


def test_plan_empty_when_up_to_date(mini_db: Path):
    _, gaps = plan_ticker_gaps(mini_db, as_of=pd.Timestamp("2026-04-10"))
    assert gaps == []


def test_synthetic_specs_cover_lev_tickers():
    synth = {s[1] for s in SYNTHETIC_SPECS}
    for name in ("LEV2QQQ", "LEV3SOXX", "LEV3SPY"):
        assert name in synth
