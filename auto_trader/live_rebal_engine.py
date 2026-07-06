"""
Live-style multi-ticker rebal simulation with JSON state persistence.

Mirrors rebal/backtest_rebal.py bar logic via drop_buy_live phases,
then applies cash-only drift rebalancing (rebal/rebal_logic.py).

Designed for:
  - daily step via shell loop (load state → process session → save state)
  - full-history replay for parity vs backtest_rebal.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_REBAL = _ROOT / "rebal"
_AUTO = Path(__file__).resolve().parent
for _p in (str(_SRC), str(_REBAL), str(_AUTO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bear_bull_drop_buy.drop_buy import Lot
from bear_bull_drop_buy.metrics import equity_curve_stats
from bear_bull_drop_buy.params import StrategyParams
from bear_bull_drop_buy.portfolio import total_equity

from drop_buy_live import (
    apply_drop_buy_buy_phase,
    apply_drop_buy_sell_phase,
)
from position_state import (
    TrackedLot,
    load_multi_state,
    multi_state_to_save,
    next_lot_id,
    tracked_lot_from_json,
)
from rebal_logic import check_drift, execute_cash_only_rebal


@dataclass
class LiveRebalResult:
    portfolio_equity: np.ndarray
    dates: List[str]
    rebal_dates: List[str]
    n_rebal_events: int
    stats: Any
    final_cash: Dict[str, float]
    final_lots_count: Dict[str, int]


def _lot_seq_key(lot_id: str) -> tuple[str, int]:
    parts = str(lot_id or "").split("-")
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return str(lot_id or ""), 0


def _sort_lots_by_lot_id(lots: list[TrackedLot]) -> list[TrackedLot]:
    return sorted(lots, key=lambda x: _lot_seq_key(x.lot_id))


def _lots_to_tracked(
    lots: List[Lot],
    prev: List[TrackedLot],
    *,
    ticker: str,
    session_compact: str,
    next_seq_ref: list[int],
    session_date_str: str,
) -> List[TrackedLot]:
    out: List[TrackedLot] = []
    used: set[str] = set()

    def _match_prev(lot: Lot) -> TrackedLot | None:
        for p in _sort_lots_by_lot_id(list(prev)):
            if p.lot_id in used:
                continue
            if abs(float(p.shares) - float(lot.shares)) < 1e-6 and abs(float(p.entry) - float(lot.entry)) < 1e-6:
                used.add(p.lot_id)
                return p
        return None

    for lot in lots:
        p = _match_prev(lot)
        if p is not None:
            out.append(TrackedLot(
                lot_id=p.lot_id,
                ticker=ticker,
                shares=float(lot.shares),
                entry=float(lot.entry),
                opened_session_date=p.opened_session_date,
                opened_at_utc=p.opened_at_utc,
                buy_reason=p.buy_reason,
                invested_usd=float(lot.shares) * float(lot.entry),
            ))
        else:
            lid = next_lot_id(session_compact, next_seq_ref[0])
            next_seq_ref[0] += 1
            out.append(TrackedLot(
                lot_id=lid,
                ticker=ticker,
                shares=float(lot.shares),
                entry=float(lot.entry),
                opened_session_date=session_date_str,
                opened_at_utc="",
                buy_reason="day_drop",
                invested_usd=float(lot.shares) * float(lot.entry),
            ))
    return _sort_lots_by_lot_id(out)


def _save_state(
    path: Path,
    tickers: List[str],
    rt: Dict[str, dict],
    cash_by_ticker: Dict[str, float],
    rebal_state: dict[str, Any],
    meta: Optional[dict[str, Any]] = None,
) -> None:
    doc = multi_state_to_save(
        strats_cash=cash_by_ticker,
        tickers=tickers,
        rt=rt,
    )
    doc["rebal"] = rebal_state
    if meta:
        doc["run_meta"] = meta
    _ensure_parent(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _init_state(
    path: Path,
    tickers: List[str],
    capital_per: float,
    run_meta: dict[str, Any],
) -> None:
    init_strats = {
        t: {
            "cash_usd": capital_per,
            "cash": capital_per,
            "lots": [],
            "last_session_date": None,
            "next_lot_seq": 1,
        }
        for t in tickers
    }
    doc = {
        "version": 4,
        "strategies": init_strats,
        "rebal": {"last_rebal_date": None, "cooldown_sessions_remaining": 0},
        "run_meta": run_meta,
        "saved_at_utc": datetime.now(tz=timezone.utc).isoformat(),
    }
    _ensure_parent(path)
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def simulate_live_rebal_path(
    ohlcv_by_ticker: Dict[str, pd.DataFrame],
    params: StrategyParams,
    tickers: List[str],
    *,
    initial_capital: float,
    rebal_threshold: float,
    rebal_cooldown_sessions: int = 20,
    need_len: int = 200,
    state_path: Path,
    meta_dir: Optional[Path] = None,
    reset_state: bool = True,
    verbose: bool = False,
) -> LiveRebalResult:
    """Replay full history with daily JSON load/save (live path)."""
    n = len(tickers)
    capital_per = initial_capital / n
    comm = params.commission
    slip = params.slippage

    common_idx: pd.DatetimeIndex | None = None
    for t in tickers:
        idx = ohlcv_by_ticker[t].index
        common_idx = idx if common_idx is None else common_idx.intersection(idx)
    common_idx = common_idx.sort_values()  # type: ignore[union-attr]

    d2i_by_ticker: Dict[str, Dict[str, int]] = {}
    closes_by_ticker: Dict[str, np.ndarray] = {}
    bull_flags_by_ticker: Dict[str, np.ndarray] = {}
    regime_min_i_by_ticker: Dict[str, int] = {}

    from bear_bull_drop_buy.regime import bull_regime_by_day

    for t in tickers:
        df = ohlcv_by_ticker[t]
        closes_by_ticker[t] = df["close"].to_numpy(dtype=float)
        bf, rmi = bull_regime_by_day(
            df, params.d_interval, params.period, params.regime_ma_type,
        )
        bull_flags_by_ticker[t] = bf
        regime_min_i_by_ticker[t] = rmi
        d2i: Dict[str, int] = {}
        for pos, row_idx in enumerate(df.index):
            ds = row_idx.date().isoformat() if hasattr(row_idx, "date") else str(row_idx)[:10]
            d2i[ds] = pos
        d2i_by_ticker[t] = d2i

    run_meta = {
        "tickers": tickers,
        "initial_capital": initial_capital,
        "rebal_threshold": rebal_threshold,
        "rebal_cooldown": rebal_cooldown_sessions,
        "need_len": need_len,
    }
    if reset_state or not state_path.is_file():
        _ensure_parent(state_path)
        _init_state(state_path, tickers, capital_per, run_meta)
    if meta_dir:
        meta_dir.mkdir(parents=True, exist_ok=True)
        for name in ("equity_log.jsonl", "rebal_events.jsonl"):
            p = meta_dir / name
            if reset_state and p.is_file():
                p.unlink()

    portfolio_equity: List[float] = []
    dates: List[str] = []
    rebal_dates: List[str] = []
    last_close: Dict[str, float] = {}

    for common_row in common_idx:
        date_str = common_row.date().isoformat() if hasattr(common_row, "date") else str(common_row)[:10]
        session_compact = date_str.replace("-", "")

        doc = load_multi_state(state_path, tickers)
        rebal_state = doc.get("rebal") or {}
        cooldown = int(rebal_state.get("cooldown_sessions_remaining", 0))

        rt: Dict[str, dict] = {}
        cash_by_ticker: Dict[str, float] = {}
        tracked_by_ticker: Dict[str, List[TrackedLot]] = {}

        for t in tickers:
            s = doc["strategies"].get(t) or {}
            cash_by_ticker[t] = float(s.get("cash", capital_per))
            tracked_by_ticker[t] = [
                tracked_lot_from_json(x, default_ticker=t) for x in (s.get("lots") or [])
            ]
            rt[t] = {
                "tracked": tracked_by_ticker[t],
                "next_seq": int(s.get("next_lot_seq") or 1),
                "last_session_date": s.get("last_session_date"),
                "cash": cash_by_ticker[t],
            }

        close_this_bar: Dict[str, float] = {}

        for t in tickers:
            df = ohlcv_by_ticker[t]
            i = d2i_by_ticker[t].get(date_str)
            if i is None:
                continue

            close_px = float(df.iloc[i]["close"])
            close_this_bar[t] = close_px
            last_close[t] = close_px

            if i == 0:
                continue

            closes = closes_by_ticker[t]
            close_px = float(closes[i])
            prev_close = float(closes[i - 1])
            regime_min_i = regime_min_i_by_ticker[t]
            bull = bool(bull_flags_by_ticker[t][i]) if i >= regime_min_i else False

            tracked = tracked_by_ticker[t]
            lot_metas = [{"lot_id": lo.lot_id} for lo in tracked]

            sell_r = apply_drop_buy_sell_phase(
                prev_close=prev_close,
                close_px=close_px,
                cash=cash_by_ticker[t],
                lots=[lo.to_lot() for lo in tracked],
                params=params,
                bull=bull,
                commission=comm,
                slippage=slip,
                lot_metas=lot_metas,
            )

            buy_r = apply_drop_buy_buy_phase(
                prev_close=prev_close,
                close_px=close_px,
                cash=sell_r.cash,
                lots=sell_r.lots,
                params=params,
                bull=bull,
                commission=comm,
                slippage=slip,
            )

            seq_ref = [rt[t]["next_seq"]]
            after_sell = _lots_to_tracked(
                sell_r.lots, tracked, ticker=t,
                session_compact=session_compact, next_seq_ref=seq_ref,
                session_date_str=date_str,
            )
            new_tracked = _lots_to_tracked(
                buy_r.lots, after_sell, ticker=t,
                session_compact=session_compact, next_seq_ref=seq_ref,
                session_date_str=date_str,
            )
            rt[t]["next_seq"] = seq_ref[0]
            rt[t]["tracked"] = new_tracked
            rt[t]["cash"] = float(buy_r.cash)
            cash_by_ticker[t] = float(buy_r.cash)
            tracked_by_ticker[t] = new_tracked

        port_eq = sum(
            cash_by_ticker[t] + sum(lo.shares for lo in tracked_by_ticker[t]) * last_close.get(t, 0.0)
            for t in tickers
        )
        portfolio_equity.append(port_eq)
        dates.append(date_str)

        rebal_ev_row: Optional[dict[str, Any]] = None
        if cooldown > 0:
            cooldown -= 1
        elif len(close_this_bar) == n:
            equity_now = {
                t: cash_by_ticker[t] + sum(lo.shares for lo in tracked_by_ticker[t]) * close_this_bar[t]
                for t in tickers
            }
            should_rebal, weights, _ = check_drift(equity_now, tickers, rebal_threshold)
            if should_rebal:
                cash_snapshot = {t: cash_by_ticker[t] for t in tickers}
                ev, _ = execute_cash_only_rebal(
                    cash_by_ticker=cash_snapshot,
                    equity_by_ticker=equity_now,
                    tickers=tickers,
                    date=date_str,
                )
                if ev is not None:
                    for t in tickers:
                        cash_by_ticker[t] = cash_snapshot[t]
                        rt[t]["cash"] = cash_by_ticker[t]
                    cooldown = rebal_cooldown_sessions
                    rebal_dates.append(date_str)
                    rebal_ev_row = {
                        "date": date_str,
                        "weights_before": ev.weights_before,
                        "weights_after": ev.weights_after,
                        "transfers": ev.transfers,
                        "cash_moved": ev.cash_moved,
                    }
                    port_eq = sum(
                        cash_by_ticker[t] + sum(lo.shares for lo in tracked_by_ticker[t]) * close_this_bar[t]
                        for t in tickers
                    )
                    portfolio_equity[-1] = port_eq
                    if verbose:
                        w_str = "  ".join(f"{k}={v*100:.1f}%" for k, v in ev.weights_before.items())
                        print(f"  [rebal] {date_str}  [{w_str}]  cash={ev.cash_moved:,.0f}")

        rebal_state_new = {
            "last_rebal_date": rebal_dates[-1] if rebal_dates else None,
            "cooldown_sessions_remaining": cooldown,
        }
        for t in tickers:
            rt[t]["last_session_date"] = date_str
        _save_state(state_path, tickers, rt, cash_by_ticker, rebal_state_new, run_meta)

        if meta_dir:
            _append_jsonl(meta_dir / "equity_log.jsonl", {
                "date": date_str,
                "portfolio_equity": round(port_eq, 4),
                **{t: round(cash_by_ticker[t] + sum(lo.shares for lo in tracked_by_ticker[t]) * last_close.get(t, 0.0), 4) for t in tickers},
            })
            if rebal_ev_row:
                _append_jsonl(meta_dir / "rebal_events.jsonl", rebal_ev_row)

    arr = np.asarray(portfolio_equity, dtype=float)
    stats = equity_curve_stats(arr, initial_capital)
    return LiveRebalResult(
        portfolio_equity=arr,
        dates=dates,
        rebal_dates=rebal_dates,
        n_rebal_events=len(rebal_dates),
        stats=stats,
        final_cash={t: float(load_multi_state(state_path, tickers)["strategies"][t].get("cash", 0.0)) for t in tickers},
        final_lots_count={t: len(load_multi_state(state_path, tickers)["strategies"][t].get("lots") or []) for t in tickers},
    )
