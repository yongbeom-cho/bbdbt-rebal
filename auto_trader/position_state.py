"""JSON state schema — per-ticker cash + lots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from strategy_core import Lot


@dataclass
class TrackedLot:
    """One fill slice."""

    lot_id: str
    ticker: str
    shares: float
    entry: float
    opened_session_date: str
    opened_at_utc: str
    buy_reason: str
    invested_usd: float

    def to_lot(self) -> Lot:
        return Lot(shares=self.shares, entry=self.entry)

    def to_json(self) -> dict[str, Any]:
        return {
            "lot_id": self.lot_id,
            "ticker": self.ticker,
            "shares": round(float(self.shares), 10),
            "entry_price": round(float(self.entry), 8),
            "opened_session_date": self.opened_session_date,
            "opened_at_utc": self.opened_at_utc,
            "buy_reason": self.buy_reason,
            "invested_usd": round(float(self.invested_usd), 8),
        }


def tracked_lot_from_json(row: dict[str, Any], *, default_ticker: str) -> TrackedLot:
    if "lot_id" not in row or not str(row.get("lot_id", "")).strip():
        raise ValueError("lot 객체에 비어 있지 않은 lot_id 가 필요합니다.")
    if "shares" not in row:
        raise ValueError("lot 객체에 shares 가 필요합니다.")
    if "entry_price" not in row:
        raise ValueError("lot 객체에 entry_price 가 필요합니다.")
    entry = float(row["entry_price"])
    return TrackedLot(
        lot_id=str(row["lot_id"]).strip(),
        ticker=str(row.get("ticker") or default_ticker),
        shares=float(row["shares"]),
        entry=entry,
        opened_session_date=str(row.get("opened_session_date") or ""),
        opened_at_utc=str(row.get("opened_at_utc") or ""),
        buy_reason=str(row.get("buy_reason") or "unknown"),
        invested_usd=float(row.get("invested_usd", row["shares"] * entry)),
    )


def default_state_document(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "strategy": "bear_bull_drop_buy",
        "cash_usd": 0.0,
        "strategy_state": {
            "cash": 0.0,
            "lots": [],
            "last_session_date": None,
            "next_lot_seq": 1,
        },
        "saved_at_utc": None,
    }


def _infer_next_seq(lots: list) -> int:
    m = 0
    for x in lots:
        if not isinstance(x, dict):
            continue
        lid = str(x.get("lot_id", ""))
        parts = lid.split("-")
        if len(parts) == 2 and parts[1].isdigit():
            m = max(m, int(parts[1]))
    return m + 1


def next_lot_id(session_date_compact: str, seq: int) -> str:
    return f"{session_date_compact}-{seq:03d}"


def append_fill(
    fills: list[dict[str, Any]],
    *,
    side: str,
    symbol: str,
    tx_date: str,
    signal_close_px: float,
    quantity_shares: float,
    avg_fill_price: float,
    gross_usd: float,
    reason: str,
    lot_id: Optional[str],
    extra: Optional[dict[str, Any]] = None,
) -> None:
    row: dict[str, Any] = {
        "side": side,
        "ticker": symbol,
        "tx_date": tx_date,
        "filled_at_utc": datetime.now(tz=timezone.utc).isoformat(),
        "signal_close_px": round(signal_close_px, 6),
        "quantity_shares": round(quantity_shares, 6),
        "avg_fill_price": round(avg_fill_price, 6),
        "gross_usd": round(gross_usd, 4),
        "reason": reason,
        "lot_id": lot_id,
    }
    if extra:
        row.update(extra)
    fills.append(row)


def default_ticker_state() -> dict[str, Any]:
    return {
        "cash_usd": 0.0,
        "cash": 0.0,
        "lots": [],
        "last_session_date": None,
        "next_lot_seq": 1,
    }


def default_rebal_state() -> dict[str, Any]:
    return {
        "last_rebal_date": None,
        "cooldown_sessions_remaining": 0,
    }


def rebal_settings_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    """Parse rebal block from auto_trader config JSON."""
    rb = cfg.get("rebal") or {}
    threshold = rb.get("threshold", rb.get("drift_pct", rb.get("rebal_threshold", 0.15)))
    if threshold is not None and float(threshold) > 1.0:
        threshold = float(threshold) / 100.0
    cooldown = rb.get("cooldown_sessions", rb.get("rebal_cooldown", 20))
    return {
        "enabled": bool(rb.get("enabled", True)),
        "threshold": float(threshold if threshold is not None else 0.15),
        "cooldown_sessions": int(cooldown),
    }


def load_multi_state(path: str | Path, tickers: list[str]) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        return {
            "version": 4,
            "strategies": {t: default_ticker_state() for t in tickers},
            "rebal": default_rebal_state(),
            "saved_at_utc": None,
        }
    with p.open(encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, dict) or "strategies" not in raw:
        return {
            "version": 4,
            "strategies": {t: default_ticker_state() for t in tickers},
            "rebal": default_rebal_state(),
            "saved_at_utc": None,
        }
    ver = int(raw.get("version") or 0)
    if ver < 2:
        return {
            "version": 4,
            "strategies": {t: default_ticker_state() for t in tickers},
            "rebal": default_rebal_state(),
            "saved_at_utc": None,
        }
    strats = raw["strategies"]
    for t in tickers:
        if t not in strats or not isinstance(strats[t], dict):
            strats[t] = default_ticker_state()
            continue
        s = strats[t]
        s["cash_usd"] = float(s.get("cash_usd") or 0.0)
        s.setdefault("lots", [])
        s.setdefault("last_session_date", None)
        s.setdefault("next_lot_seq", _infer_next_seq(s.get("lots") or []))
        if s.get("cash") is None:
            s["cash"] = float(s.get("cash_usd") or 0.0)
    raw["version"] = 4
    raw.setdefault("rebal", default_rebal_state())
    return raw


def multi_state_to_save(
    *,
    strats_cash: dict[str, float],
    tickers: list[str],
    rt: dict[str, Any],
    rebal_state: Optional[dict[str, Any]] = None,
    run_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    out_strategies: dict[str, Any] = {}
    for t in tickers:
        r = rt.get(t, {})
        tracked: list[TrackedLot] = r.get("tracked", [])
        out_strategies[t] = {
            "cash_usd": round(float(strats_cash.get(t, 0.0)), 8),
            "cash": round(float(r.get("cash", strats_cash.get(t, 0.0))), 8),
            "lots": [lo.to_json() for lo in tracked],
            "last_session_date": r.get("last_session_date"),
            "next_lot_seq": int(r.get("next_seq", 1)),
        }
    doc: dict[str, Any] = {
        "version": 4,
        "strategies": out_strategies,
        "saved_at_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    if rebal_state is not None:
        doc["rebal"] = rebal_state
    if run_meta is not None:
        doc["run_meta"] = run_meta
    return doc
