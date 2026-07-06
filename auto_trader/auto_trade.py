#!/usr/bin/env python3
"""
USA ETF bear-bull drop-buy multi-ticker live trading.

Based on short-term-trading-quant/auto_trader execution shell; strategy logic
matches bear_bull_drop_buy.backtest.run_backtest (sell phase then buy phase per bar).
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

_AUTO = Path(__file__).resolve().parent
_ROOT = _AUTO.parent
_SRC = _ROOT / "src"
_REBAL = _ROOT / "rebal"
for _p in (_SRC, _REBAL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import koreainvestment as mojito
from broker_flow import (
    cancel_all_open_orders,
    fetch_ohlcv_paged,
    flush_pending_orders,
    get_balance_info,
    log_exc,
    market_buy_limit,
    market_sell_limit,
)
from bear_bull_drop_buy.params import StrategyParams
from drop_buy_live import (
    apply_drop_buy_buy_phase,
    apply_drop_buy_sell_phase,
    bull_regime_last_bar,
    strategy_params_from_config,
)
from position_state import (
    TrackedLot,
    append_fill,
    default_rebal_state,
    load_multi_state,
    multi_state_to_save,
    next_lot_id,
    rebal_settings_from_config,
    tracked_lot_from_json,
)
from rebal_logic import check_drift, execute_cash_only_rebal
from strategy_core import Lot

load_dotenv(_AUTO / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _lots_to_tracked(
    lots: list[Lot],
    prev: list[TrackedLot],
    *,
    ticker: str,
) -> list[TrackedLot]:
    """Preserve lot_id by matching shares+entry."""
    out: list[TrackedLot] = []
    used: set[str] = set()

    def _match_prev(lot: Lot) -> TrackedLot | None:
        for p in _sort_lots_by_lot_id(list(prev)):
            if p.lot_id in used:
                continue
            if abs(float(p.shares) - float(lot.shares)) < 1e-6 and abs(float(p.entry) - float(lot.entry)) < 1e-6:
                used.add(p.lot_id)
                return p
        return None

    for i, lot in enumerate(lots):
        p = _match_prev(lot)
        if p is not None:
            out.append(
                TrackedLot(
                    lot_id=p.lot_id,
                    ticker=ticker,
                    shares=float(lot.shares),
                    entry=float(lot.entry),
                    opened_session_date=p.opened_session_date,
                    opened_at_utc=p.opened_at_utc,
                    buy_reason=p.buy_reason,
                    invested_usd=float(lot.shares) * float(lot.entry),
                )
            )
        else:
            out.append(
                TrackedLot(
                    lot_id=f"sync-{i:03d}",
                    ticker=ticker,
                    shares=float(lot.shares),
                    entry=float(lot.entry),
                    opened_session_date="",
                    opened_at_utc="",
                    buy_reason="phase_sync",
                    invested_usd=float(lot.shares) * float(lot.entry),
                )
            )
    return _sort_lots_by_lot_id(out)



def send_discord(url: str, msg: str) -> None:
    if not url:
        print(msg)
        return
    try:
        requests.post(url, data={"content": msg[:1900]}, timeout=10)
    except OSError:
        print(msg)
    print("MSG:", msg)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_fills_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    rows = [r for r in rows if float(r.get("quantity_shares") or 0) > 0]
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False))
            f.write("\n")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False))
        f.write("\n")


def _persist_state(
    path: Path,
    *,
    cash_by_ticker: dict[str, float],
    tickers: list[str],
    rt: dict[str, dict[str, Any]],
    rebal_state: dict[str, Any],
    run_meta: Optional[dict[str, Any]],
) -> None:
    save_json(
        path,
        multi_state_to_save(
            strats_cash=cash_by_ticker,
            tickers=tickers,
            rt=rt,
            rebal_state=rebal_state,
            run_meta=run_meta,
        ),
    )


def _ensure_session_closes(
    *,
    tickers: list[str],
    hist_by_ticker: dict[str, Any],
    rt: dict[str, dict[str, Any]],
    broker: mojito.KoreaInvestment,
    ticker_to_exchange: dict[str, str],
    need_len: int,
    retry_cnt: int,
) -> dict[str, float]:
    closes: dict[str, float] = {}
    for t in tickers:
        if t in hist_by_ticker:
            closes[t] = float(hist_by_ticker[t]["close"].iloc[-1])
            continue
        last_px = rt.get(t, {}).get("last_px")
        if last_px is not None:
            closes[t] = float(last_px)
            continue
        ex = ticker_to_exchange[t]
        hist = fetch_ohlcv_paged(
            broker, t, exchange=ex, need_length=need_len, adj_price=True, retry_cnt=retry_cnt,
        )
        if hist is not None and len(hist) >= 2:
            closes[t] = float(hist["close"].iloc[-1])
    return closes


def run_eod_rebal_if_needed(
    *,
    tickers: list[str],
    cash_by_ticker: dict[str, float],
    rt: dict[str, dict[str, Any]],
    close_by_ticker: dict[str, float],
    rebal_cfg: dict[str, Any],
    rebal_state: dict[str, Any],
    session_date_str: str,
    discord_url: str,
    rebal_events_path: Optional[Path],
) -> dict[str, Any]:
    if not rebal_cfg.get("enabled"):
        log.info("rebal disabled in config → skip")
        return rebal_state

    cooldown = int(rebal_state.get("cooldown_sessions_remaining", 0))
    if cooldown > 0:
        rebal_state["cooldown_sessions_remaining"] = cooldown - 1
        log.info("rebal cooldown  %d → %d", cooldown, cooldown - 1)
        return rebal_state

    if len(close_by_ticker) < len(tickers):
        missing = [t for t in tickers if t not in close_by_ticker]
        log.warning("rebal skip: missing close for %s", missing)
        return rebal_state

    equity_now = {
        t: float(cash_by_ticker[t]) + sum(float(lo.shares) for lo in rt_get_tracked(rt[t])) * close_by_ticker[t]
        for t in tickers
    }
    should_rebal, weights, _excess = check_drift(equity_now, tickers, float(rebal_cfg["threshold"]))
    if not should_rebal:
        w_str = "  ".join(f"{k}={v*100:.1f}%" for k, v in weights.items())
        log.info("rebal not triggered  [%s]  threshold=%.2f%%", w_str, rebal_cfg["threshold"] * 100)
        return rebal_state

    cash_snapshot = {t: float(cash_by_ticker[t]) for t in tickers}
    ev, _eq_after = execute_cash_only_rebal(
        cash_by_ticker=cash_snapshot,
        equity_by_ticker=equity_now,
        tickers=tickers,
        date=session_date_str,
    )
    if ev is None:
        log.info("rebal drift triggered but no cash transfer executed")
        return rebal_state

    for t in tickers:
        cash_by_ticker[t] = cash_snapshot[t]
        rt[t]["cash"] = cash_snapshot[t]

    rebal_state["last_rebal_date"] = session_date_str
    rebal_state["cooldown_sessions_remaining"] = int(rebal_cfg["cooldown_sessions"])

    w_before = "  ".join(f"{k}={v*100:.1f}%" for k, v in ev.weights_before.items())
    w_after = "  ".join(f"{k}={v*100:.1f}%" for k, v in ev.weights_after.items())
    tf = "  ".join(f"{k}:{v:+.0f}" for k, v in ev.transfers.items())
    msg = (
        f"[rebal] {session_date_str}  before=[{w_before}]  after=[{w_after}]  "
        f"cash_moved={ev.cash_moved:,.0f}  [{tf}]"
    )
    log.info(msg)
    send_discord(discord_url, msg)

    if rebal_events_path is not None:
        append_jsonl(
            rebal_events_path,
            {
                "date": session_date_str,
                "weights_before": ev.weights_before,
                "weights_after": ev.weights_after,
                "transfers": ev.transfers,
                "cash_moved": ev.cash_moved,
                "threshold": rebal_cfg["threshold"],
            },
        )
    return rebal_state


def minutes_to_us_regular_close(ny_now: datetime, *, close_hhmm: str) -> float:
    seg = close_hhmm.strip().split(":")
    h, m = int(seg[0]), int(seg[1]) if len(seg) > 1 else 0
    close_today = datetime.combine(ny_now.date(), dtime(h, m), tzinfo=ny_now.tzinfo)
    if ny_now <= close_today:
        return (close_today - ny_now).total_seconds() / 60.0
    nxt = ny_now.date() + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    close_next = datetime.combine(nxt, dtime(h, m), tzinfo=ny_now.tzinfo)
    return (close_next - ny_now).total_seconds() / 60.0


def refresh_broker(
    cfg: dict[str, Any],
    broker_holder: list[Optional[mojito.KoreaInvestment]],
    resp: Optional[dict] = None,
) -> None:
    if resp is not None and resp.get("rt_cd") == "1":
        log.warning("refresh_broker: rt_cd=1 → token.dat 삭제 후 재발급")
        p = Path.cwd() / "token.dat"
        if p.is_file():
            p.unlink(missing_ok=True)
    for attempt in range(int(cfg.get("retry_cnt", 5))):
        try:
            broker_holder[0] = mojito.KoreaInvestment(
                api_key=cfg["app_key"],
                api_secret=cfg["app_secret"],
                acc_no=cfg["acc_no"],
                mock=False,
            )
            if cfg.get("api_url"):
                broker_holder[0].base_url = str(cfg["api_url"])
            log.info("refresh_broker: OK  attempt=%d", attempt + 1)
            return
        except OSError:
            log.warning("refresh_broker: OSError  attempt=%d", attempt + 1)
            time.sleep(0.2)
    log.error("refresh_broker: 모든 시도 실패")


def rt_get_tracked(r: dict[str, Any]) -> list[TrackedLot]:
    return list(r.get("tracked", []))


def rt_set_tracked(r: dict[str, Any], tracked: list[TrackedLot]) -> None:
    r["tracked"] = list(tracked)


def _tracked_notional_flat(tracked: list[TrackedLot]) -> float:
    return sum(float(t.shares) * float(t.entry) for t in tracked)


def _qty_avg_from_ticker_info(ti: dict[str, Any]) -> tuple[float, float]:
    q = float(ti.get("quantity", 0) or 0)
    ba = float(ti.get("buy_amount", 0) or 0)
    if q <= 1e-9:
        return 0.0, 0.0
    return q, ba / q


def _lot_seq_key(lot_id: str) -> tuple[str, int]:
    parts = str(lot_id or "").split("-")
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return str(lot_id or ""), 0


def _sort_lots_by_lot_id(lots: list[TrackedLot]) -> list[TrackedLot]:
    return sorted(lots, key=lambda x: _lot_seq_key(x.lot_id))


def _total_shares(tracked: list[TrackedLot]) -> float:
    return sum(float(x.shares) for x in tracked)




def _entry_notional(tracked: list[TrackedLot]) -> float:
    return sum(float(x.shares) * float(x.entry) for x in tracked)


def _remove_shares_from_lots(
    tracked: list[TrackedLot],
    qty: float,
    *,
    prefer_lot_id: Optional[str],
) -> tuple[list[TrackedLot], float]:
    out = _sort_lots_by_lot_id(list(tracked))
    rem = max(0.0, float(qty))
    eps = 1e-9

    if rem <= eps:
        return out, 0.0

    # Prefer exact lot_id first (fill-linked rollback), then tail-lots (latest first).
    if prefer_lot_id:
        for i, lo in enumerate(out):
            if str(lo.lot_id) == str(prefer_lot_id):
                s = float(lo.shares)
                take = min(s, rem)
                lo.shares = s - take
                lo.invested_usd = float(lo.shares) * float(lo.entry)
                rem -= take
                if lo.shares <= eps:
                    out.pop(i)
                break

    i = len(out) - 1
    while rem > eps and i >= 0:
        s = float(out[i].shares)
        take = min(s, rem)
        out[i].shares = s - take
        out[i].invested_usd = float(out[i].shares) * float(out[i].entry)
        rem -= take
        if out[i].shares <= eps:
            out.pop(i)
        i -= 1
    return out, rem


def _add_shares_to_lots(
    tracked: list[TrackedLot],
    qty: float,
    *,
    prefer_lot_id: Optional[str],
    entry: float,
    symbol: str,
    tx_date: str,
    next_seq: int,
) -> tuple[list[TrackedLot], int]:
    out = _sort_lots_by_lot_id(list(tracked))
    q = max(0.0, float(qty))
    if q <= 1e-9:
        return out, next_seq

    # First: exact lot_id (if exists), else newest lot, else create.
    if prefer_lot_id:
        for lo in reversed(out):
            if str(lo.lot_id) == str(prefer_lot_id):
                lo.shares = float(lo.shares) + q
                if float(lo.entry) <= 0 and entry > 0:
                    lo.entry = float(entry)
                lo.invested_usd = float(lo.shares) * float(lo.entry)
                return out, next_seq

    for lo in reversed(out):
        if True:
            lo.shares = float(lo.shares) + q
            if float(lo.entry) <= 0 and entry > 0:
                lo.entry = float(entry)
            lo.invested_usd = float(lo.shares) * float(lo.entry)
            return out, next_seq

    lid = prefer_lot_id if prefer_lot_id else next_lot_id(tx_date.replace("-", ""), next_seq)
    if not prefer_lot_id:
        next_seq += 1
    nowu = datetime.now(tz=timezone.utc).isoformat()
    out.append(
        TrackedLot(
            lot_id=str(lid),
            ticker=symbol,
            shares=q,
            entry=max(0.0, float(entry)),
            opened_session_date=tx_date,
            opened_at_utc=nowu,
            buy_reason="sync_revert_sell",
            invested_usd=q * max(0.0, float(entry)),
        )
    )
    return _sort_lots_by_lot_id(out), next_seq


def load_recent_fills_by_ticker(path: Path, tickers: list[str], *, max_n: int = 4) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in tickers}
    if max_n <= 0 or (not path.is_file()):
        return out

    by_ticker_by_date: dict[str, dict[str, list[dict[str, Any]]]] = {t: {} for t in tickers}
    with path.open(encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            try:
                row = json.loads(s)
            except json.JSONDecodeError:
                continue
            t = str(row.get("ticker") or "")
            if t not in by_ticker_by_date:
                continue
            tx_date = str(row.get("tx_date") or row.get("session_date") or "")
            if not tx_date:
                continue
            by_ticker_by_date[t].setdefault(tx_date, []).append(row)

    for t in tickers:
        dmap = by_ticker_by_date.get(t) or {}
        if not dmap:
            continue
        last_date = sorted(dmap.keys())[-1]
        rows = dmap[last_date]
        out[t] = rows[-max_n:]
    return out


def apply_recent_fill_scenarios_to_match_qty(
    *,
    tracked: list[TrackedLot],
    symbol: str,
    broker_qty: float,
    next_seq: int,
    recent_fills: list[dict[str, Any]],
    cash: float,
) -> tuple[list[TrackedLot], int, bool, bool, float]:
    """
    Try 2^N rollback scenarios (N<=4 recent fills); choose one matching broker qty.
    Bit=1 keep fill applied, bit=0 revert this fill.
    """
    eps = 1e-6
    fills = [x for x in (recent_fills or []) if str(x.get("ticker") or "") == symbol]
    fills = [x for x in fills if float(x.get("quantity_shares", 0) or 0) > eps]
    if not fills:
        return tracked, next_seq, False, False, float(cash)

    n = len(fills)
    best: Optional[tuple[tuple[float, int], list[TrackedLot], int, float]] = None
    target_qty = max(0.0, float(broker_qty))

    for mask in range(1 << n):
        lots = _sort_lots_by_lot_id(copy.deepcopy(tracked))
        seq = int(next_seq)
        c = float(cash)
        reverted_cnt = 0
        valid = True
        for i, row in enumerate(fills):
            keep_applied = ((mask >> i) & 1) == 1
            if keep_applied:
                continue
            reverted_cnt += 1
            side = str(row.get("side") or "").lower()
            qty = float(row.get("quantity_shares") or 0.0)
            lot_id = str(row.get("lot_id") or "") or None
            tx_date = str(row.get("tx_date") or row.get("session_date") or "")
            if not tx_date:
                tx_date = datetime.now(tz=timezone.utc).date().isoformat()

            if side == "buy":
                # Revert buy: remove shares from local lots.
                lots, rem = _remove_shares_from_lots(lots, qty, prefer_lot_id=lot_id)
                if rem > eps:
                    valid = False
                    break
                gross = float(row.get("gross_usd") or 0.0)
                c += gross
            elif side == "sell":
                # Revert sell: add shares back to local lots.
                entry = float(row.get("entry_price_at_buy") or row.get("avg_fill_price") or 0.0)
                lots, seq = _add_shares_to_lots(
                    lots,
                    qty,
                    prefer_lot_id=lot_id,
                    entry=entry,
                    symbol=symbol,
                    tx_date=tx_date,
                    next_seq=seq,
                )
                gross = float(row.get("gross_usd") or 0.0)
                # Rule B: cash 부족이면 시나리오 invalid
                if c + eps < gross:
                    valid = False
                    break
                c = max(0.0, c - gross)
            else:
                valid = False
                break

        if not valid:
            continue
        diff = abs(_total_shares(lots) - target_qty)
        key = (diff, reverted_cnt)
        if best is None or key < best[0]:
            best = (key, lots, seq, c)

    if best is None:
        return tracked, next_seq, False, False, float(cash)

    diff, reverted_cnt = best[0]
    chosen_lots = best[1]
    chosen_seq = best[2]
    chosen_c = best[3]
    if diff <= 1e-6 and (_total_shares(chosen_lots) - _total_shares(tracked)) != 0:
        log.info(
            "sync[%s]: recent fills(2^%d) 복원 시나리오 채택  reverted=%d  qty=%.4f",
            symbol,
            n,
            reverted_cnt,
            _total_shares(chosen_lots),
        )
        return chosen_lots, chosen_seq, True, True, chosen_c
    return tracked, next_seq, False, True, float(cash)


def _force_sync_qty(
    *,
    tracked: list[TrackedLot],
    symbol: str,
    target_qty: float,
    broker_avg: float,
    session_compact: str,
    session_date_str: str,
    next_seq: int,
) -> tuple[list[TrackedLot], int, bool]:
    eps = 1e-9
    out = _sort_lots_by_lot_id(list(tracked))
    cur_qty = _total_shares(out)
    if abs(cur_qty - target_qty) <= eps:
        return out, next_seq, False
    changed = False
    if cur_qty > target_qty + eps:
        out, rem = _remove_shares_from_lots(out, cur_qty - target_qty, prefer_lot_id=None)
        changed = rem <= eps
    else:
        add = target_qty - cur_qty
        if out:
            out[-1].shares = float(out[-1].shares) + add
            out[-1].invested_usd = float(out[-1].shares) * float(out[-1].entry)
            changed = True
        else:
            lid = next_lot_id(session_compact, next_seq)
            next_seq += 1
            nowu = datetime.now(tz=timezone.utc).isoformat()
            out.append(
                TrackedLot(
                    lot_id=lid,
                    ticker=symbol,
                    shares=add,
                    entry=float(broker_avg),
                    opened_session_date=session_date_str,
                    opened_at_utc=nowu,
                    buy_reason="force_sync_qty_add",
                    invested_usd=add * float(broker_avg),
                )
            )
            changed = True
    return _sort_lots_by_lot_id(out), next_seq, changed



def sync_tracked_with_broker(
    *,
    tracked: list[TrackedLot],
    symbol: str,
    broker_qty: float,
    broker_avg: float,
    session_compact: str,
    session_date_str: str,
    next_seq: int,
    recent_fills: Optional[list[dict[str, Any]]] = None,
    cash: float = 0.0,
    ignore_residual_threshold: float = 0.0,
    skip_zero_delete: bool = False,
) -> tuple[list[TrackedLot], int, bool, bool, float]:
    eps = 1e-6
    out = list(tracked)
    changed = False
    target_qty = max(0.0, float(broker_qty))
    target_notional = max(0.0, float(broker_qty) * float(broker_avg))

    if target_qty <= eps:
        if out:
            if skip_zero_delete:
                log.warning(
                    "sync[%s]: 브로커 잔고 0이지만 skip_zero_delete=True → lot %d개 유지 (미체결 추정)",
                    symbol,
                    len(out),
                )
                return out, next_seq, False, False, float(cash)
            log.info("sync[%s]: 브로커 잔고 0 → 로컬 lot %d개 전부 제거", symbol, len(out))
            return [], next_seq, True, False, float(cash)
        return out, next_seq, False, False, float(cash)

    reverted, seq2, rv_changed, rv_attempted, cash_after = apply_recent_fill_scenarios_to_match_qty(
        tracked=out,
        symbol=symbol,
        broker_qty=target_qty,
        next_seq=next_seq,
        recent_fills=recent_fills or [],
        cash=float(cash),
    )
    if rv_changed:
        out = reverted
        next_seq = seq2
        changed = True
        cash = cash_after

    if not out:
        if ignore_residual_threshold > 0 and target_qty <= ignore_residual_threshold:
            log.warning(
                "sync[%s]: 잔량 %.4f ≤ 무시기준 %.4f → lot 미생성 (매도 후 잔량 무시)",
                symbol,
                target_qty,
                ignore_residual_threshold,
            )
            return [], next_seq, False, rv_attempted, float(cash)
        lid = next_lot_id(session_compact, next_seq)
        next_seq += 1
        nowu = datetime.now(tz=timezone.utc).isoformat()
        log.info(
            "sync[%s]: 로컬 lot 없음 → 브로커 기준 신규 lot  lid=%s  qty=%.4f  avg=%.4f",
            symbol,
            lid,
            target_qty,
            broker_avg,
        )
        return [
            TrackedLot(
                lot_id=lid,
                ticker=symbol,
                shares=target_qty,
                entry=float(broker_avg),
                opened_session_date=session_date_str,
                opened_at_utc=nowu,
                buy_reason="sync_from_broker",
                invested_usd=target_notional,
            )
        ], next_seq, True, rv_attempted, float(cash)

    cur_qty = sum(float(t.shares) for t in out)
    if rv_attempted and not rv_changed and abs(cur_qty - target_qty) > eps:
        forced, seq3, forced_changed = _force_sync_qty(
            tracked=out,
            symbol=symbol,
            target_qty=target_qty,
            broker_avg=broker_avg,
            session_compact=session_compact,
            session_date_str=session_date_str,
            next_seq=next_seq,
        )
        if forced_changed:
            out = forced
            next_seq = seq3
            changed = True
            cur_qty = _total_shares(out)
            log.warning("sync[%s]: fills 시나리오 불일치 → 강제 수량 sync 적용", symbol)

    if cur_qty + eps < target_qty:
        add_sh = target_qty - cur_qty
        if out:
            out[-1].shares = float(out[-1].shares) + add_sh
            out[-1].invested_usd = float(out[-1].shares) * float(out[-1].entry)
            log.info(
                "sync[%s]: 로컬 qty(%.4f) < 브로커(%.4f)  차이(%.4f) → 마지막 lot 주수 보정",
                symbol,
                cur_qty,
                target_qty,
                add_sh,
            )
            changed = True
        else:
            lid = next_lot_id(session_compact, next_seq)
            next_seq += 1
            nowu = datetime.now(tz=timezone.utc).isoformat()
            out.append(
                TrackedLot(
                    lot_id=lid,
                    ticker=symbol,
                    shares=add_sh,
                    entry=float(broker_avg),
                    opened_session_date=session_date_str,
                    opened_at_utc=nowu,
                    buy_reason="sync_qty_add",
                    invested_usd=add_sh * float(broker_avg),
                )
            )
            log.info("sync[%s]: 로컬 lot 없음·차이만 존재 → lot 추가  lid=%s", symbol, lid)
            changed = True
    elif cur_qty > target_qty + eps:
        rem = cur_qty - target_qty
        log.info("sync[%s]: 로컬 qty(%.4f) > 브로커(%.4f) → 초과분(%.4f) 제거", symbol, cur_qty, target_qty, rem)
        i = len(out) - 1
        while rem > eps and i >= 0:
            s = float(out[i].shares)
            if s <= rem + eps:
                rem -= s
                out.pop(i)
            else:
                out[i].shares = s - rem
                out[i].invested_usd = out[i].shares * out[i].entry
                rem = 0.0
            changed = True
            i -= 1

    if not out:
        lid = next_lot_id(session_compact, next_seq)
        next_seq += 1
        nowu = datetime.now(tz=timezone.utc).isoformat()
        return [
            TrackedLot(
                lot_id=lid,
                ticker=symbol,
                shares=target_qty,
                entry=float(broker_avg),
                opened_session_date=session_date_str,
                opened_at_utc=nowu,
                buy_reason="sync_from_broker",
                invested_usd=target_notional,
            )
        ], next_seq, True, rv_attempted, float(cash)

    if float(broker_avg) > 0:
        cur_notional = _tracked_notional_flat(out)
        delta = target_notional - cur_notional
        if abs(delta) > 1e-4:
            last = out[-1]
            if float(last.shares) > eps:
                last.entry = max(0.0, float(last.entry) + delta / float(last.shares))
                log.info("sync[%s]: notional 조정  delta=%.4f  last.entry=%.4f", symbol, delta, last.entry)
                changed = True
    else:
        log.info("sync[%s]: broker_avg=0 → notional 조정 스킵 (sim entry 유지)", symbol)

    for t in out:
        t.invested_usd = float(t.shares) * float(t.entry)
        t.ticker = symbol
    return out, next_seq, changed, rv_attempted, float(cash)


def sync_ticker_lot_and_cash(
    *,
    t: str,
    rt_entry: dict,
    cash_by_ticker: dict[str, float],
    tickers: list[str],
    session_compact: str,
    session_date_str: str,
    get_broker_cash_and_position_fn,
    label: str,
    params: StrategyParams,
    broker=None,
    exchange: str = "",
    sync_retry: int = 3,
    ignore_residual_threshold: float = 0.0,
    skip_zero_delete: bool = False,
) -> tuple[float, float]:
    if broker is not None:
        for attempt in range(sync_retry):
            n = flush_pending_orders(broker, t, exchange=exchange)
            if n == 0:
                break
            log.info("%s  미체결 %d건 정정 (attempt %d/%d)", label, n, attempt + 1, sync_retry)
            time.sleep(2)
        else:
            m = cancel_all_open_orders(broker, t, exchange=exchange)
            log.warning("%s  retry %d회 초과 → 미체결 %d건 전부 취소", label, sync_retry, m)
            time.sleep(2)

    broker_cash, ticker_info = get_broker_cash_and_position_fn(t)
    brk_qty, brk_avg = _qty_avg_from_ticker_info(ticker_info)
    log.info(
        "sync_ticker_lot_and_cash - %s  broker_cash=%.2f  broker_qty=%.4f  broker_avg=%.4f",
        label,
        broker_cash,
        brk_qty,
        brk_avg,
    )
    flat = rt_get_tracked(rt_entry)
    synced, next_seq, changed, _rv_attempted, cash2 = sync_tracked_with_broker(
        tracked=flat,
        symbol=t,
        broker_qty=brk_qty,
        broker_avg=brk_avg,
        session_compact=session_compact,
        session_date_str=session_date_str,
        next_seq=rt_entry["next_seq"],
        cash=float(rt_entry.get("cash", 0.0)),
        ignore_residual_threshold=ignore_residual_threshold,
        skip_zero_delete=skip_zero_delete,
    )
    if changed:
        rt_set_tracked(rt_entry, synced)
        rt_entry["next_seq"] = next_seq

    others = [x for x in tickers if x != t]
    new_cash = (broker_cash - sum(cash_by_ticker[x] for x in others)) if others else broker_cash
    if new_cash < 0:
        log.warning(
            "%s  cash 음수  broker_cash=%.2f  others_sum=%.2f → 0 클램핑",
            label,
            broker_cash,
            sum(cash_by_ticker[x] for x in others),
        )
        new_cash = 0.0
    log.info("%s  broker_cash=%.2f  cash[%s]=%.2f", label, broker_cash, t, new_cash)

    rt_entry["cash"] = float(cash2)

    cash_by_ticker[t] = new_cash

    return _tracked_notional_flat(rt_get_tracked(rt_entry)), new_cash


def consistency_check_lot_vs_cash(
    *,
    t: str,
    lot_notional_before: float,
    lot_notional_after: float,
    cash_before: float,
    cash_after: float,
    phase_label: str,
    discord_url: str,
    expected_cash_delta: Optional[float] = None,
) -> None:
    lot_delta = lot_notional_after - lot_notional_before
    # expected_cash_delta가 주어지면 현재가 기준 예상값 사용, 없으면 entry 가격 기준 fallback
    expected = expected_cash_delta if expected_cash_delta is not None else -lot_delta
    actual = cash_after - cash_before
    denom = max(abs(expected), 10.0)
    rel = abs(actual - expected) / denom
    if rel >= 0.05:
        msg = (
            f"[consistency_warn] {t} {phase_label}: "
            f"lot_Δnotional={lot_delta:.2f} "
            f"expected_cash_Δ={expected:.2f}  actual_cash_Δ={actual:.2f}  rel={rel * 100:.2f}%"
        )
        log.warning(msg)
        send_discord(discord_url, msg)


def sync_local_cash_and_position_info_with_broker(
    *,
    broker_total_cash: float,
    broker_positions: dict[str, Any],
    cash_by_ticker: dict[str, float],
    rt: dict[str, Any],
    tickers: list[str],
    recent_fills_by_ticker: dict[str, list],
    session_compact: str,
    session_date_str: str,
    label: str,
) -> None:
    """Lot + cash sync in one pass. Modifies cash_by_ticker and rt in-place.

    Phase 1: sync lot shares/notional per ticker via sync_tracked_with_broker.
    Phase 2: redistribute broker_total_cash across tickers proportionally.
    Single cash per ticker.
    """
    # Phase 1: lot sync
    for t in tickers:
        r = rt[t]
        brk_qty, brk_avg = _qty_avg_from_ticker_info(broker_positions.get(t, {}))
        flat = rt_get_tracked(r)
        local_qty = _total_shares(flat)
        recent = [] if abs(local_qty - brk_qty) <= 1e-4 else recent_fills_by_ticker.get(t, [])
        synced, next_seq, changed, _, _cash = sync_tracked_with_broker(
            tracked=flat,
            symbol=t,
            broker_qty=brk_qty,
            broker_avg=brk_avg,
            session_compact=session_compact,
            session_date_str=session_date_str,
            next_seq=int(r.get("next_seq", 1)),
            recent_fills=recent,
            cash=float(r.get("cash", 0.0)),
        )
        if changed:
            log.info("[%s] %s lot 동기화됨  local_qty=%.4f  broker_qty=%.4f", t, label, local_qty, brk_qty)
        rt_set_tracked(r, synced)
        r["next_seq"] = next_seq

    # Phase 2: cash redistribution
    total_local = sum(cash_by_ticker.get(t, 0.0) for t in tickers)
    delta = broker_total_cash - total_local
    log.info("%s  broker_cash=%.2f  sum_local=%.2f  delta=%.2f", label, broker_total_cash, total_local, delta)
    if abs(delta) > 0.01:
        n = len(tickers)
        if delta > 0:
            per_t = delta / max(n, 1)
            for t in tickers:
                cash_by_ticker[t] = cash_by_ticker.get(t, 0.0) + per_t
                log.info("  [%s] cash +%.2f → %.2f", t, per_t, cash_by_ticker[t])
        else:
            to_drain = -delta
            active = list(tickers)
            result = {t: cash_by_ticker.get(t, 0.0) for t in tickers}
            eps = 1e-6
            while to_drain > eps and active:
                per_share = to_drain / len(active)
                next_active: list[str] = []
                actually_drained = 0.0
                for t in active:
                    if result[t] >= per_share - eps:
                        result[t] = max(0.0, result[t] - per_share)
                        actually_drained += per_share
                        next_active.append(t)
                    else:
                        log.info("  [%s] 현금 부족(%.2f < %.2f) → 전액 차감 후 탈락", t, result[t], per_share)
                        actually_drained += result[t]
                        result[t] = 0.0
                to_drain -= actually_drained
                active = next_active
            if to_drain > 0.01:
                log.warning("%s  차감 후 잔차 %.2f 미반영 (전 전략 현금 소진)", label, to_drain)
            for t in tickers:
                log.info("  [%s] cash %.2f → %.2f", t, cash_by_ticker.get(t, 0.0), result[t])
                cash_by_ticker[t] = result[t]

    for t in tickers:
        rt[t]["cash"] = cash_by_ticker[t]
        log.info("[%s] %s  cash=%.2f", t, label, cash_by_ticker[t])


def run() -> None:
    ap = argparse.ArgumentParser(description="USA ETF bear-bull drop-buy multi-strategy live trading")
    ap.add_argument("--config", type=Path, default=_AUTO / "config.json")
    ap.add_argument("--state", type=Path, default=_AUTO / "position_state_multi.json")
    ap.add_argument("--fills", type=Path, default=_AUTO / "fills.jsonl")
    ap.add_argument("--rebal-events", type=Path, default=_AUTO / "rebal_events.jsonl")
    ap.add_argument("--force", action="store_true", help="같은 미국 거래일에도 다시 실행")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="주문 API 호출 없이 계획만 로그/디스코드 (실거래 검증용)",
    )
    args = ap.parse_args()

    cfg = load_json(args.config)
    cfg["app_key"] = os.getenv("app_key", cfg.get("app_key", ""))
    cfg["app_secret"] = os.getenv("app_secret", cfg.get("app_secret", ""))
    cfg["acc_no"] = os.getenv("acc_no", cfg.get("acc_no", ""))
    cfg["api_url"] = os.getenv("api_url", cfg.get("api_url", ""))
    if not cfg["app_key"] or not cfg["app_secret"] or not cfg["acc_no"]:
        print("app_key, app_secret, acc_no 환경변수 또는 config 필요", file=sys.stderr)
        sys.exit(1)

    discord_url = os.getenv("discord_url", cfg.get("discord_url", ""))
    wh_env = cfg.get("discord_webhook_env", "DISCORD_WEBHOOK_URL")
    if not discord_url:
        discord_url = os.getenv(wh_env, cfg.get("discord_url", ""))

    strategies_cfg: list[dict[str, str]] = cfg["strategies"]
    tickers: list[str] = [s["ticker"] for s in strategies_cfg]
    ticker_to_exchange = {s["ticker"]: s["exchange"] for s in strategies_cfg}
    recent_fills_by_ticker = load_recent_fills_by_ticker(args.fills, tickers, max_n=4)
    for t in tickers:
        if recent_fills_by_ticker.get(t):
            txd = str(recent_fills_by_ticker[t][-1].get("tx_date") or recent_fills_by_ticker[t][-1].get("session_date") or "")
            log.info("[%s] recent fills loaded: n=%d  tx_date=%s", t, len(recent_fills_by_ticker[t]), txd or "unknown")

    params = strategy_params_from_config(dict(cfg.get("strategy_params") or {}))
    params.validate()
    rebal_cfg = rebal_settings_from_config(cfg)
    run_meta = cfg.get("run_meta")

    comm = float(cfg.get("commission", params.commission))
    slip = float(cfg.get("slippage", params.slippage))
    need_len = int(cfg.get("ohlcv_bars", 400))
    retry_cnt = int(cfg.get("retry_cnt", 5))
    chunk_limit_usd = float(cfg.get("chunk_limit_usd", 50_000))
    close_hhmm = str(cfg.get("us_regular_close_hhmm", "16:00"))
    target_m = float(cfg.get("minutes_before_close", 10))
    dry_run = bool(args.dry_run)

    log.info(
        "설정 로드  config=%s  tickers=%s  comm=%.4f  slip=%.4f  need_len=%d  chunk_limit=%.0f  dry_run=%s",
        args.config.name,
        tickers,
        comm,
        slip,
        need_len,
        chunk_limit_usd,
        dry_run,
    )
    if rebal_cfg["enabled"]:
        log.info(
            "rebal enabled  threshold=%.2f%%  cooldown=%d sessions",
            rebal_cfg["threshold"] * 100,
            rebal_cfg["cooldown_sessions"],
        )
    else:
        log.info("rebal disabled")

    ny = ZoneInfo("America/New_York")
    us_now = datetime.now(ny)
    us_date = us_now.date()
    session_date_str = us_date.isoformat()
    session_compact = us_date.strftime("%Y%m%d")
    log.info("NY시각=%s  us_date=%s  weekday=%d", us_now.strftime("%H:%M:%S"), session_date_str, us_date.weekday())

    if us_date.weekday() >= 5 and not cfg.get("allow_weekend_run"):
        send_discord(discord_url, f"[drop_buy] 스킵: 미국 주말 ({us_date})")
        return

    broker_box: list[Optional[mojito.KoreaInvestment]] = [None]
    log.info("브로커 초기화")
    refresh_broker(cfg, broker_box)

    def refresh_for_balance(
        cfg_b: dict[str, Any],
        holder: list[Optional[mojito.KoreaInvestment]],
        resp: Optional[dict],
    ) -> None:
        refresh_broker(cfg_b, holder, resp)

    def make_order_refresh_fn():
        def fn(resp: dict) -> mojito.KoreaInvestment:
            refresh_broker(cfg, broker_box, resp)
            return broker_box[0]

        return fn

    def get_broker_cash_and_position(ticker: str) -> tuple[float, dict[str, Any]]:
        _tev, cash, holds = get_balance_info(
            broker_box,
            cfg=cfg,
            retry_cnt=retry_cnt,
            refresh_broker_fn=refresh_for_balance,
        )
        _ = _tev
        return cash, holds.get(ticker, {})

    def get_broker_cash() -> float:
        c, _ti = get_broker_cash_and_position(tickers[0])
        return c

    def get_broker_position(ticker: str) -> tuple[float, float]:
        _, ti = get_broker_cash_and_position(ticker)
        return _qty_avg_from_ticker_info(ti)

    doc = load_multi_state(args.state, tickers)
    strats_state = doc["strategies"]
    rebal_state: dict[str, Any] = dict(doc.get("rebal") or default_rebal_state())

    cash_by_ticker: dict[str, float] = {t: float(strats_state[t]["cash_usd"]) for t in tickers}

    log.info("상태 로드  tickers=%s", tickers)
    for t in tickers:
        s = strats_state[t]
        log.info(
            "  [%s] cash=%.2f  lots=%d  last_session=%s",
            t,
            cash_by_ticker[t],
            len(s.get("lots") or []),
            s.get("last_session_date"),
        )

    log.info("브로커 초기 잔고 조회...")
    _tev_init, broker_cash_init, broker_holds_init = get_balance_info(
        broker_box, cfg=cfg, retry_cnt=retry_cnt, refresh_broker_fn=refresh_for_balance
    )
    log.info("계좌 현금=%.2f", broker_cash_init)

    rt: dict[str, dict[str, Any]] = {}
    for t in tickers:
        s = strats_state[t]
        rt[t] = {
            "tracked": [tracked_lot_from_json(x, default_ticker=t) for x in (s.get("lots") or [])],
            "next_seq": int(s.get("next_lot_seq") or 1),
            "last_session_date": s.get("last_session_date"),
            "cash": float(s.get("cash") or s.get("cash_usd") or 0.0),
        }

    sync_local_cash_and_position_info_with_broker(
        broker_total_cash=broker_cash_init,
        broker_positions=broker_holds_init,
        cash_by_ticker=cash_by_ticker,
        rt=rt,
        tickers=tickers,
        recent_fills_by_ticker=recent_fills_by_ticker,
        session_compact=session_compact,
        session_date_str=session_date_str,
        label="[초기 sync]",
        )

    for t in tickers:
        cash_by_ticker[t] = float(rt[t]["cash"])
        for i, lo in enumerate(rt_get_tracked(rt[t])):
            log.info(
                "  [%s] lot[%d]  id=%s  shares=%.4f  entry=%.4f  reason=%s",
                t,
                i,
                lo.lot_id,
                lo.shares,
                lo.entry,
                lo.buy_reason,
            )

    _persist_state(
        args.state,
        cash_by_ticker=cash_by_ticker,
        tickers=tickers,
        rt=rt,
        rebal_state=rebal_state,
        run_meta=run_meta,
    )
    log.info("초기 동기화 상태 저장 완료")

    sleep_interval_s = int(cfg.get("pre_close_sleep_seconds", 60))
    log.info("장마감 대기 시작  target_m=%.1f분 전  sleep=%ds", target_m, sleep_interval_s)
    _wait_loop_count = 0
    _log_every_n = max(1, 3600 // sleep_interval_s)
    while True:
        us_now = datetime.now(ny)
        mins_left = minutes_to_us_regular_close(us_now, close_hhmm=close_hhmm)
        if _wait_loop_count % _log_every_n == 0:
            log.info("대기 중  NY=%s  장마감까지 %.1f분", us_now.strftime("%H:%M:%S"), mins_left)
        _wait_loop_count += 1
        if mins_left <= target_m:
            break
        time.sleep(sleep_interval_s)

    us_now = datetime.now(ny)
    us_date = us_now.date()
    session_date_str = us_date.isoformat()
    session_compact = us_date.strftime("%Y%m%d")
    log.info("장마감 window 진입  NY=%s  session=%s", us_now.strftime("%H:%M:%S"), session_date_str)

    target_tickers: list[str] = []
    for t in tickers:
        if rt[t].get("last_session_date") == session_date_str and not args.force:
            log.info("[%s] 이미 처리한 세션 %s → 스킵", t, session_date_str)
            send_discord(discord_url, f"[drop_buy] {t} 이미 처리한 세션 {session_date_str} — 스킵")
        else:
            target_tickers.append(t)

    if not target_tickers:
        send_discord(discord_url, f"[drop_buy] 모든 전략 이미 처리됨 {session_date_str}")
        return
    log.info("처리 대상: %s", target_tickers)

    fills_new: list[dict[str, Any]] = []
    hist_by_ticker: dict[str, Any] = {}

    for t in list(target_tickers):
        ex = ticker_to_exchange[t]
        broker = broker_box[0]
        assert broker is not None
        log.info("[%s] OHLCV 로딩  need_len=%d  exchange=%s", t, need_len, ex)
        hist = fetch_ohlcv_paged(broker, t, exchange=ex, need_length=need_len, adj_price=True, retry_cnt=retry_cnt)
        if hist is None or len(hist) < 30:
            send_discord(discord_url, f"[drop_buy] {t} OHLCV 로드 실패 → 이 전략 스킵")
            log.error("[%s] OHLCV 로드 실패", t)
            target_tickers.remove(t)
            continue
        log.info("[%s] OHLCV 완료  행=%d  범위=%s ~ %s", t, len(hist), hist["date"].iloc[0], hist["date"].iloc[-1])
        hist_by_ticker[t] = hist

    if not target_tickers:
        send_discord(discord_url, "[drop_buy] OHLCV 로드 전략 없음 → 종료")
        return

    log.info("장마감 직전 잔고 재조회...")
    _tev_pre, broker_cash_pre, broker_holds_pre = get_balance_info(
        broker_box, cfg=cfg, retry_cnt=retry_cnt, refresh_broker_fn=refresh_for_balance
    )
    sync_local_cash_and_position_info_with_broker(
        broker_total_cash=broker_cash_pre,
        broker_positions=broker_holds_pre,
        cash_by_ticker=cash_by_ticker,
        rt=rt,
        tickers=tickers,
        recent_fills_by_ticker=recent_fills_by_ticker,
        session_compact=session_compact,
        session_date_str=session_date_str,
        label="[장마감 직전 sync]",
        )
    for t in tickers:
        cash_by_ticker[t] = float(rt[t]["cash"])

    for t in target_tickers:
        hist = hist_by_ticker[t]
        last_px = float(hist["close"].iloc[-1])
        prev_close = float(hist["close"].iloc[-2])
        bull = bull_regime_last_bar(hist, params)
        r = rt[t]

        metas = [dict(lo.to_json()) for lo in r["tracked"]]

        log.info("[%s] 매도 페이즈  prev_close=%.4f  close=%.4f  cash=%.2f  lots=%d  bull=%s", t, prev_close, last_px, r["cash"], len(r["tracked"]), bull)

        sell_r = apply_drop_buy_sell_phase(
            prev_close=prev_close,
            close_px=last_px,
            cash=float(r["cash"]),
            lots=[lo.to_lot() for lo in r["tracked"]],
            params=params,
            bull=bull,
            commission=comm,
            slippage=slip,
            lot_metas=metas,
        )
        log.info("[%s] 매도 계획=%d건", t, len(sell_r.sell_plans))
        send_discord(
            discord_url,
            f"[drop_buy] {t} session={session_date_str} close≈{last_px:.4f} bull={bull} 매도건수={len(sell_r.sell_plans)}",
        )

        ex = ticker_to_exchange[t]
        broker = broker_box[0]
        assert broker is not None

        for idx, plan in enumerate(sell_r.sell_plans):
            q = int(plan.shares)
            if q <= 0:
                continue

            lot_notional_before, cash_before = sync_ticker_lot_and_cash(
                t=t,
                rt_entry=r,
                cash_by_ticker=cash_by_ticker,
                tickers=tickers,
                session_compact=session_compact,
                session_date_str=session_date_str,
                get_broker_cash_and_position_fn=get_broker_cash_and_position,
                label=f"[{t}] sell plan[{idx}] 전",
                params=params,
                broker=broker,
                exchange=ex,
            )

            log.info("[%s] 매도 주문 시작  총qty=%d  reason=%s  dry_run=%s", t, q, plan.reason, dry_run)
            q_remain = q
            lot_id_sell = plan.lot_meta.get("lot_id") if plan.lot_meta else None
            while q_remain > 0:
                chunk_qty = min(q_remain, max(1, int(chunk_limit_usd / max(last_px, 1e-6))))
                if dry_run:
                    sold_qty = int(chunk_qty)
                    sell_resp = {"rt_cd": "0", "dry_run": True}
                else:
                    sold_qty, sell_resp = market_sell_limit(
                        broker,
                        t,
                        chunk_qty,
                        exchange=ex,
                        retry_cnt=retry_cnt,
                        refresh_broker_fn=make_order_refresh_fn(),
                    )
                    broker = broker_box[0]
                gross = float(chunk_qty) * last_px
                log.info("[%s] 매도 chunk  qty=%d  sold=%d  rt=%s", t, chunk_qty, sold_qty, sell_resp.get("rt_cd"))
                append_fill(
                    fills_new,
                    side="sell",
                    symbol=t,
                    tx_date=session_date_str,
                    signal_close_px=last_px,
                    quantity_shares=float(chunk_qty),
                    avg_fill_price=last_px,
                    gross_usd=gross,
                    reason=plan.reason,
                    lot_id=str(lot_id_sell) if lot_id_sell else None,
                    extra={"entry_price_at_buy": plan.entry},
                )
                send_discord(
                    discord_url,
                    f"[drop_buy] sell {t} chunk={chunk_qty} reason={plan.reason} rt={sell_resp.get('rt_cd')}",
                )
                q_remain -= chunk_qty
                if q_remain > 0:
                    time.sleep(3)
            time.sleep(3)

            lot_notional_after, cash_after = sync_ticker_lot_and_cash(
                t=t,
                rt_entry=r,
                cash_by_ticker=cash_by_ticker,
                tickers=tickers,
                session_compact=session_compact,
                session_date_str=session_date_str,
                get_broker_cash_and_position_fn=get_broker_cash_and_position,
                label=f"[{t}] sell plan[{idx}] 후",
                params=params,
                broker=broker,
                exchange=ex,
            )
            consistency_check_lot_vs_cash(
                t=t,
                lot_notional_before=lot_notional_before,
                lot_notional_after=lot_notional_after,
                cash_before=cash_before,
                cash_after=cash_after,
                phase_label=f"sell plan[{idx}]",
                discord_url=discord_url,
                expected_cash_delta=float(q) * last_px,
            )

        r["cash"] = float(sell_r.cash)
        rt_set_tracked(r, _lots_to_tracked(sell_r.lots, r["tracked"], ticker=t))

        r["last_px"] = last_px
        r["prev_close"] = prev_close
        r["bull"] = bull

    for t in target_tickers:
        r = rt[t]
        last_px = float(r["last_px"])
        prev_close = float(r["prev_close"])
        bull = bool(r["bull"])

        log.info("[%s] 매수 페이즈  cash=%.2f  lots=%d  bull=%s", t, r["cash"], len(r["tracked"]), bull)

        buy_r = apply_drop_buy_buy_phase(
            prev_close=prev_close,
            close_px=last_px,
            cash=float(r["cash"]),
            lots=[lo.to_lot() for lo in r["tracked"]],
            params=params,
            bull=bull,
            commission=comm,
            slippage=slip,
        )
        log.info("[%s] 매수 계획=%d건", t, len(buy_r.buy_plans))
        send_discord(
            discord_url,
            f"[drop_buy] {t} 매수 시그널={len(buy_r.buy_plans)}건 (전략 현금≈{cash_by_ticker[t]:.2f})",
        )

        ex = ticker_to_exchange[t]
        broker = broker_box[0]
        assert broker is not None

        ticker_did_merge = False

        for j, plan in enumerate(buy_r.buy_plans):
            sim_lot = Lot(
                shares=float(plan.shares),
                entry=float(plan.entry),
            )
            fills_start_idx = len(fills_new)
            merge_prev_lot_id = r["tracked"][-1].lot_id if r["tracked"] else None
            seq_before_plan = r["next_seq"]

            lot_notional_before, cash_before = sync_ticker_lot_and_cash(
                t=t,
                rt_entry=r,
                cash_by_ticker=cash_by_ticker,
                tickers=tickers,
                session_compact=session_compact,
                session_date_str=session_date_str,
                get_broker_cash_and_position_fn=get_broker_cash_and_position,
                label=f"[{t}] buy plan[{j}] 전",
                params=params,
                broker=broker,
                exchange=ex,
            )

            pre_buy_qty, pre_buy_avg = get_broker_position(t)
            pre_buy_notional = pre_buy_qty * pre_buy_avg
            log.info("[%s] buy plan[%d] 전  broker_qty=%.4f  broker_avg=%.4f", t, j, pre_buy_qty, pre_buy_avg)

            s_remain = float(plan.spend_usd)
            lid: Optional[str] = None
            nowu = datetime.now(tz=timezone.utc).isoformat()
            any_fill = False
            plan_spend = 0.0
            log.info("[%s] 매수 plan[%d]  spend_usd=%.2f  reason=%s", t, j, s_remain, plan.reason)

            while s_remain > 1.0:
                strat_remain = cash_by_ticker[t] - plan_spend
                broker_cash_cur = get_broker_cash()
                chunk = min(s_remain, chunk_limit_usd, max(0.0, min(strat_remain, broker_cash_cur) * 0.99))
                log.info("  [%s] chunk  strat_remain=%.2f  broker=%.2f  chunk=%.2f", t, strat_remain, broker_cash_cur, chunk)
                if chunk < 1.0:
                    log.warning("  [%s] 매수 중단: 현금 부족  need~%.2f  chunk=%.2f", t, s_remain, chunk)
                    send_discord(discord_url, f"[drop_buy] {t} buy 중단: 현금 부족 need~{s_remain:.2f}")
                    break
                if lid is None:
                    lid = next_lot_id(session_compact, r["next_seq"])
                    r["next_seq"] += 1
                log.info("  [%s] 매수 주문  chunk=%.2f  lid=%s  dry_run=%s", t, chunk, lid, dry_run)
                if dry_run:
                    qty = int(max(1.0, chunk / max(last_px, 1e-6)))
                    resp = {"rt_cd": "0", "dry_run": True}
                else:
                    qty, resp = market_buy_limit(
                        broker,
                        t,
                        chunk,
                        exchange=ex,
                        retry_cnt=retry_cnt,
                        refresh_broker_fn=make_order_refresh_fn(),
                    )
                    broker = broker_box[0]
                fill_px = last_px
                gross = float(qty) * fill_px
                log.info(
                    "  [%s] 매수 결과  qty=%s  rt_cd=%s  msg=%s  gross=%.2f",
                    t,
                    qty,
                    resp.get("rt_cd"),
                    resp.get("msg1", ""),
                    gross,
                )
                if qty > 0:
                    any_fill = True
                    plan_spend += chunk
                append_fill(
                    fills_new,
                    side="buy",
                    symbol=t,
                    tx_date=session_date_str,
                    signal_close_px=last_px,
                    quantity_shares=float(qty),
                    avg_fill_price=fill_px,
                    gross_usd=gross,
                    reason=plan.reason,
                    lot_id=lid,
                    extra={"order_chunk_usd": round(chunk, 4)},
                )
                send_discord(
                    discord_url,
                    f"[drop_buy] buy {t} chunk={chunk:.2f} qty={qty} lot={lid} rt={resp.get('rt_cd')}",
                )
                s_remain -= chunk

            if any_fill and lid is not None and not dry_run:
                time.sleep(5)
                post_buy_qty, post_buy_avg = get_broker_position(t)
                post_buy_notional = post_buy_qty * post_buy_avg
                delta_qty = post_buy_qty - pre_buy_qty
                actual_entry = float(sim_lot.entry)
                if delta_qty > 1e-6 and post_buy_avg > 0:
                    actual_entry = (post_buy_notional - pre_buy_notional) / delta_qty
                    log.info(
                        "[%s] 매수 평단 역산  pre=%.2f×%.4f  post=%.2f×%.4f  delta_qty=%.4f  actual_entry=%.4f",
                        t,
                        pre_buy_qty,
                        pre_buy_avg,
                        post_buy_qty,
                        post_buy_avg,
                        delta_qty,
                        actual_entry,
                    )
                else:
                    log.warning(
                        "[%s] 매수 평단 역산 불가 (delta_qty=%.4f  post_avg=%.4f) → sim_entry=%.4f 사용",
                        t,
                        delta_qty,
                        post_buy_avg,
                        actual_entry,
                    )

                if round(delta_qty) == 1 and merge_prev_lot_id is not None and delta_qty > 1e-6:
                    prev_lo = r["tracked"][-1]
                    new_shares = float(prev_lo.shares) + float(delta_qty)
                    prev_lo.entry = (float(prev_lo.shares) * float(prev_lo.entry) + float(delta_qty) * actual_entry) / new_shares
                    prev_lo.shares = new_shares
                    prev_lo.invested_usd = new_shares * prev_lo.entry
                    r["next_seq"] = seq_before_plan
                    for row in fills_new[fills_start_idx:]:
                        row["lot_id"] = merge_prev_lot_id
                    ticker_did_merge = True
                    log.info("[%s] 1주 잔돈 매수 → 직전 lot(%s) 합산  shares=%.4f  entry=%.4f", t, prev_lo.lot_id, prev_lo.shares, prev_lo.entry)
                else:
                    new_lo = TrackedLot(
                        lot_id=lid,
                        ticker=t,
                        shares=float(sim_lot.shares),
                        entry=float(actual_entry),
                        opened_session_date=session_date_str,
                        opened_at_utc=nowu,
                        buy_reason=plan.reason,
                        invested_usd=float(plan.spend_usd),
                    )
                    r["tracked"].append(new_lo)
            elif float(plan.spend_usd) > 1.0:
                log.warning("[%s] 매수 체결 없음  plan[%d]  reason=%s", t, j, plan.reason)
                send_discord(discord_url, f"[drop_buy] 경고: {t} 매수({plan.reason}) 체결 없음")

            lot_notional_after, cash_after = sync_ticker_lot_and_cash(
                t=t,
                rt_entry=r,
                cash_by_ticker=cash_by_ticker,
                tickers=tickers,
                session_compact=session_compact,
                session_date_str=session_date_str,
                get_broker_cash_and_position_fn=get_broker_cash_and_position,
                label=f"[{t}] buy plan[{j}] 후",
                params=params,
                broker=broker,
                exchange=ex,
                skip_zero_delete=True,
            )
            consistency_check_lot_vs_cash(
                t=t,
                lot_notional_before=lot_notional_before,
                lot_notional_after=lot_notional_after,
                cash_before=cash_before,
                cash_after=cash_after,
                phase_label=f"buy plan[{j}]",
                discord_url=discord_url,
            )

        r["cash"] = float(buy_r.cash)
        sim_lots = list(buy_r.lots)
        if ticker_did_merge and len(sim_lots) >= 2 and r["tracked"]:
            sim_lots.pop()
            tl = r["tracked"][-1]
            sim_lots[-1] = Lot(shares=float(tl.shares), entry=float(tl.entry))
        rt_set_tracked(r, _lots_to_tracked(sim_lots, r["tracked"], ticker=t))
        r["last_session_date"] = session_date_str

        log.info("[%s] 매수 완료  lots=%d  cash=%.2f", t, len(r["tracked"]), cash_by_ticker[t])

    broker = broker_box[0]
    assert broker is not None
    close_by_ticker = _ensure_session_closes(
        tickers=tickers,
        hist_by_ticker=hist_by_ticker,
        rt=rt,
        broker=broker,
        ticker_to_exchange=ticker_to_exchange,
        need_len=need_len,
        retry_cnt=retry_cnt,
    )
    rebal_state = run_eod_rebal_if_needed(
        tickers=tickers,
        cash_by_ticker=cash_by_ticker,
        rt=rt,
        close_by_ticker=close_by_ticker,
        rebal_cfg=rebal_cfg,
        rebal_state=rebal_state,
        session_date_str=session_date_str,
        discord_url=discord_url,
        rebal_events_path=args.rebal_events,
    )

    log.info("최종 잔고 재조회...")
    _tev_fin, broker_cash_fin, broker_holds_fin = get_balance_info(
        broker_box, cfg=cfg, retry_cnt=retry_cnt, refresh_broker_fn=refresh_for_balance
    )
    sync_local_cash_and_position_info_with_broker(
        broker_total_cash=broker_cash_fin,
        broker_positions=broker_holds_fin,
        cash_by_ticker=cash_by_ticker,
        rt=rt,
        tickers=tickers,
        recent_fills_by_ticker=recent_fills_by_ticker,
        session_compact=session_compact,
        session_date_str=session_date_str,
        label="[최종 sync]",
    )
    for t in tickers:
        cash_by_ticker[t] = float(rt[t]["cash"])

    _persist_state(
        args.state,
        cash_by_ticker=cash_by_ticker,
        tickers=tickers,
        rt=rt,
        rebal_state=rebal_state,
        run_meta=run_meta,
    )
    append_fills_jsonl(args.fills, fills_new)
    log.info("저장 완료  path=%s  fills=%d건  rebal_cooldown=%d", args.state, len(fills_new), rebal_state.get("cooldown_sessions_remaining", 0))
    send_discord(
        discord_url,
        f"[drop_buy] 전략={tickers}  세션={session_date_str}  저장={args.state}  fills={len(fills_new)}건",
    )


if __name__ == "__main__":
    try:
        run()
    except Exception:
        print(log_exc(), file=sys.stderr)
        sys.exit(1)