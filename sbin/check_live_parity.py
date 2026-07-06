#!/usr/bin/env python3
"""
auto_trader 경로 vs backtest 패리티 검증 (bear-bull-drop-buy).

auto_trade.py 일일 사이클을 브로커 없이 재현:
  1. position_state.json (load_multi_state / multi_state_to_save) 읽기/쓰기
  2. TrackedLot → to_lot() → apply_sell/buy_phase → _lots_to_tracked 변환
  3. bull_regime_last_bar(ohlcv[last need_len], params) — live와 동일한 창 크기
  4. 완전 체결 가정 (close 가격, 브로커 sync 없음)

Usage:
    python sbin/check_live_parity.py [--ticker SOXL] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parents[1]
_SRC  = _ROOT / "src"
_AUTO = _ROOT / "auto_trader"
for p in (_SRC, _AUTO):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bear_bull_drop_buy.backtest    import run_backtest
from bear_bull_drop_buy.data_loader import load_period, default_project_db_path
from bear_bull_drop_buy.metrics     import (
    annualized_cagr_trading_days,
    buy_and_hold_stats,
    equity_curve_stats,
)
from bear_bull_drop_buy.params      import StrategyParams

from drop_buy_live import (
    apply_drop_buy_buy_phase,
    apply_drop_buy_sell_phase,
    bull_regime_last_bar,
    strategy_params_from_config,
)
from position_state import (
    TrackedLot,
    load_multi_state,
    multi_state_to_save,
    next_lot_id,
    tracked_lot_from_json,
)
from strategy_core import Lot


# ─── auto_trade.py 에서 복사한 helpers ──────────────────────────────────────

def _lot_seq_key(lot_id: str) -> tuple[str, int]:
    parts = str(lot_id or "").split("-")
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], int(parts[1])
    return str(lot_id or ""), 0


def _sort_lots_by_lot_id(lots: list[TrackedLot]) -> list[TrackedLot]:
    return sorted(lots, key=lambda x: _lot_seq_key(x.lot_id))


def _lots_to_tracked(
    lots: list[Lot],
    prev: list[TrackedLot],
    *,
    ticker: str,
    session_compact: str,
    next_seq_ref: list[int],
    session_date_str: str,
) -> list[TrackedLot]:
    """auto_trade.py _lots_to_tracked 와 동일 로직.

    새 lot은 session_compact + next_seq_ref[0]으로 고유 lot_id를 부여하고
    next_seq_ref[0]을 증가시킨다 (자동매매 auto_trade.py 와 동일).
    """
    out: list[TrackedLot] = []
    used: set[str] = set()

    def _match_prev(lot: Lot) -> TrackedLot | None:
        for p in _sort_lots_by_lot_id(list(prev)):
            if p.lot_id in used:
                continue
            if (abs(float(p.shares) - float(lot.shares)) < 1e-6
                    and abs(float(p.entry) - float(lot.entry)) < 1e-6):
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


# ─── state 저장 헬퍼 ─────────────────────────────────────────────────────────

def _save_state(path: Path, ticker: str, rt_entry: dict, cash: float) -> None:
    doc = multi_state_to_save(
        strats_cash={ticker: cash},
        tickers=[ticker],
        rt={ticker: rt_entry},
    )
    with path.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)


# ─── 일일 사이클 시뮬레이션 ──────────────────────────────────────────────────

def _simulate_auto_trader_path(
    df: "pd.DataFrame",
    params: StrategyParams,
    initial_capital: float,
    ticker: str,
    state_path: Path,
    need_len: int = 100,
    verbose: bool = False,
) -> tuple[list[float], list[dict], list[str]]:
    """
    auto_trade.py 일일 사이클을 DB OHLCV 로 재현한다.

    bar i 에서 df.iloc[max(0, i+1-need_len) : i+1] 를 슬라이스해
    live와 동일하게 need_len개 창으로 신호를 계산한다.
    매일 position_state.json 을 읽고 쓰며 JSON 직렬화 왕복을 포함한다.
    """
    comm = params.commission
    slip = params.slippage

    # 초기 state 생성
    init_doc = {
        "version": 4,
        "strategies": {
            ticker: {
                "cash_usd": float(initial_capital),
                "cash":     float(initial_capital),
                "lots":     [],
                "last_session_date": None,
                "next_lot_seq": 1,
            }
        },
        "saved_at_utc": None,
    }
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(init_doc, f, ensure_ascii=False, indent=2)

    closes = df["close"].to_numpy(dtype=float)
    equity_curve: list[float] = []
    all_events:   list[dict]  = []

    for i in range(len(df)):
        session_dt       = df.index[i]
        session_date_str = (
            session_dt.date().isoformat()
            if hasattr(session_dt, "date")
            else str(session_dt)[:10]
        )
        session_compact = session_date_str.replace("-", "")
        close_px = float(closes[i])

        # ① position_state.json 로드
        doc  = load_multi_state(state_path, [ticker])
        s    = doc["strategies"][ticker]
        cash = float(s.get("cash") or s.get("cash_usd") or 0.0)
        tracked: list[TrackedLot] = [
            tracked_lot_from_json(x, default_ticker=ticker)
            for x in (s.get("lots") or [])
        ]
        next_seq = int(s.get("next_lot_seq") or 1)

        # 첫 bar는 prev_close 없음 → equity MTM만 기록
        if i == 0:
            mtm = cash + sum(lo.shares * close_px for lo in tracked)
            equity_curve.append(mtm)
            _save_state(state_path, ticker, {
                "tracked": tracked, "next_seq": next_seq,
                "last_session_date": session_date_str, "cash": cash,
            }, cash)
            continue

        # ② 최근 need_len 개 bar 슬라이스 (live: broker에서 fetch한 것과 동일)
        slice_start = max(0, i + 1 - need_len)
        ohlcv_slice = df.iloc[slice_start: i + 1]

        prev_close = float(ohlcv_slice["close"].iloc[-2])
        bull = bull_regime_last_bar(ohlcv_slice, params)

        # ③ 매도 페이즈 (auto_trade.py 와 동일 순서)
        sell_r = apply_drop_buy_sell_phase(
            prev_close=prev_close,
            close_px=close_px,
            cash=cash,
            lots=[lo.to_lot() for lo in tracked],
            params=params,
            bull=bull,
            commission=comm,
            slippage=slip,
        )
        for plan in sell_r.sell_plans:
            all_events.append({"kind": plan.reason, "date": session_date_str})

        # ④ 매수 페이즈
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
        for plan in buy_r.buy_plans:
            all_events.append({"kind": plan.reason, "date": session_date_str})

        # ⑤ lots → TrackedLot 변환 (auto_trade.py 동일)
        # next_seq_ref는 mutable list로 참조 전달 (새 lot 생성 시 증가)
        seq_ref = [next_seq]
        after_sell_tracked = _lots_to_tracked(
            sell_r.lots, tracked, ticker=ticker,
            session_compact=session_compact, next_seq_ref=seq_ref,
            session_date_str=session_date_str,
        )

        # auto_trade.py 1주 잔돈 merge 시뮬레이션:
        # 정수 주수 매매로 인한 잔돈이 가격 하락 후 1주를 살 수 있게 되면
        # 새 lot을 만들지 않고 직전 lot에 합산한다 (auto_trade.py 동일 로직).
        sim_lots = list(buy_r.lots)
        if buy_r.buy_plans and after_sell_tracked and len(sim_lots) >= 2:
            plan0 = buy_r.buy_plans[0]
            sim_qty = int(plan0.spend_usd / (close_px * (1.0 + comm + slip)))
            if sim_qty == 1:
                sim_lots.pop()
                new_sh = float(sim_lots[-1].shares) + 1.0
                new_en = (float(sim_lots[-1].shares) * float(sim_lots[-1].entry) + close_px) / new_sh
                sim_lots[-1] = Lot(shares=new_sh, entry=new_en)

        new_tracked = _lots_to_tracked(
            sim_lots, after_sell_tracked, ticker=ticker,
            session_compact=session_compact, next_seq_ref=seq_ref,
            session_date_str=session_date_str,
        )
        next_seq = seq_ref[0]

        cash = float(buy_r.cash)

        if verbose:
            evkinds = [e["kind"] for e in all_events if e["date"] == session_date_str]
            if any(k not in ("",) for k in evkinds):
                print(f"  {session_date_str}  close={close_px:.4f}  cash={cash:.4f}"
                      f"  lots={len(new_tracked)}  bull={bull}  events={evkinds}")

        # ⑥ state 저장
        _save_state(state_path, ticker, {
            "tracked":           new_tracked,
            "next_seq":          next_seq,
            "last_session_date": session_date_str,
            "cash":              cash,
        }, cash)

        # ⑦ EOD equity (MTM)
        mtm = cash + sum(lo.shares * close_px for lo in new_tracked)
        equity_curve.append(mtm)

    event_kinds = [e["kind"] for e in all_events]
    return equity_curve, all_events, event_kinds


# ─── 메인 ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker",  default="SOXL")
    ap.add_argument("--start",   default="2010-01-01")
    ap.add_argument("--end",     default="2026-06-13")
    ap.add_argument("--capital", type=float, default=1.0)
    ap.add_argument("--config",  type=Path, default=_ROOT / "configs" / "viewer_default.json")
    ap.add_argument("--need-len", type=int, default=0,
                    help="OHLCV slice length for live sim (0 = from config ohlcv_bars or 100)")
    ap.add_argument("--warmup",  type=int, default=300)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # ── 파라미터 로드 ─────────────────────────────────────────────────────────
    with args.config.open(encoding="utf-8") as f:
        cfg = json.load(f)

    # viewer_default.json 은 strategy_params 래핑 없이 flat 구조
    params_src = cfg.get("strategy_params") or cfg
    params = strategy_params_from_config(dict(params_src))
    params.validate()

    need_len = args.need_len or int(cfg.get("ohlcv_bars", 100))
    print(f"params: regime_ma_type={params.regime_ma_type}  d_interval={params.d_interval}"
          f"  period={params.period}  ticker={args.ticker}")
    print(f"need_len(live slice): {need_len}")

    # ── OHLCV DB 로드 ────────────────────────────────────────────────────────
    db = default_project_db_path()
    df_full, df_eval = load_period(
        db, args.ticker, f"{args.start}:{args.end}", warmup_bars=args.warmup
    )
    n_days = len(df_full)
    print(f"full : {df_full.index[0].date()} ~ {df_full.index[-1].date()}  ({n_days}거래일, 워밍업 포함)")
    print(f"eval : {df_eval.index[0].date()} ~ {df_eval.index[-1].date()}  ({len(df_eval)}거래일)\n")

    # ── ① backtest (기준값) ───────────────────────────────────────────────────
    bt = run_backtest(df_full, params, initial_capital=args.capital, trade_log=True)
    bt_kinds  = [e["kind"] for e in (bt.trade_events or [])]
    bt_equity = np.asarray(bt.equity_curve, dtype=float)
    bt_stats  = equity_curve_stats(bt_equity, args.capital)
    bt_cagr   = annualized_cagr_trading_days(bt_stats.total_pnl, n_days)

    # ── ② auto_trader 경로 시뮬 ──────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        state_path = Path(tf.name)

    live_equity_raw, live_events, live_kinds = _simulate_auto_trader_path(
        df_full, params, args.capital, args.ticker, state_path,
        need_len=need_len, verbose=args.verbose,
    )
    state_path.unlink(missing_ok=True)

    live_equity = np.asarray(live_equity_raw, dtype=float)
    live_stats  = equity_curve_stats(live_equity, args.capital)
    live_cagr   = annualized_cagr_trading_days(live_stats.total_pnl, n_days)

    # ── ③ Buy & Hold ──────────────────────────────────────────────────────────
    bh = buy_and_hold_stats(
        df_full["close"].to_numpy(dtype=float),
        initial_capital=args.capital,
        commission=params.commission,
        slippage=params.slippage,
    )
    bh_cagr = annualized_cagr_trading_days(bh.total_pnl, n_days)

    # ── 결과 출력 ──────────────────────────────────────────────────────────────
    print("=" * 57)
    print(f"{'항목':<24} {'Backtest':>10} {'AutoTrader':>10}")
    print("-" * 57)
    print(f"{'최종 equity':<24} {bt_stats.final_equity:>10.6f} {live_stats.final_equity:>10.6f}")
    print(f"{'Annualized CAGR (%)':<24} {bt_cagr*100:>10.2f} {live_cagr*100:>10.2f}")
    print(f"{'MDD (%)':<24} {bt_stats.max_drawdown_pct:>10.2f} {live_stats.max_drawdown_pct:>10.2f}")
    print(f"{'이벤트 수':<24} {len(bt_kinds):>10} {len(live_kinds):>10}")
    print("=" * 57)
    print(f"\nBuy & Hold  CAGR={bh_cagr*100:.2f}%  MDD={bh.max_drawdown_pct:.2f}%")

    # ── 이벤트 시퀀스 비교 ───────────────────────────────────────────────────
    # backtest와 live는 동일 액션에 다른 이름을 사용 → 정규화 후 비교
    _EVENT_NORM: dict[str, str] = {
        "drop_buy":               "buy",
        "day_drop":               "buy",
        "drop_sell_take_profit":  "sell_tp",
        "drop_take_profit_lot":   "sell_tp",
        "drop_sell_surge":        "sell_surge",
        "drop_surge_partial_newest": "sell_surge",
    }
    def _norm(kinds: list[str]) -> list[str]:
        return [_EVENT_NORM.get(k, k) for k in kinds]

    bt_norm   = _norm(bt_kinds)
    live_norm = _norm(live_kinds)
    print()
    if bt_norm == live_norm:
        print("✓ 이벤트 시퀀스 일치 (이름 정규화 후)")
    else:
        print("✗ 이벤트 시퀀스 불일치!")
        for idx, (bk, lk) in enumerate(zip(bt_norm, live_norm)):
            if bk != lk:
                print(f"  첫 불일치 index={idx}  bt={bt_kinds[idx]}({bk})  live={live_kinds[idx]}({lk})")
                print(f"  bt   context: {bt_kinds[max(0,idx-2):idx+3]}")
                print(f"  live context: {live_kinds[max(0,idx-2):idx+3]}")
                break
        if len(bt_norm) != len(live_norm):
            print(f"  이벤트 수  bt={len(bt_kinds)}  live={len(live_kinds)}")

    # ── equity 오차 ───────────────────────────────────────────────────────────
    min_len  = min(len(bt_equity), len(live_equity))
    abs_diff = float(np.max(np.abs(bt_equity[:min_len] - live_equity[:min_len])))
    denom    = np.maximum(np.abs(bt_equity[:min_len]), 1e-9)
    rel_diff = float(np.max(np.abs(
        (bt_equity[:min_len] - live_equity[:min_len]) / denom
    )))

    print(f"\nEquity 최대 절대오차: {abs_diff:.10f}")
    print(f"Equity 최대 상대오차: {rel_diff*100:.8f}%")

    if rel_diff < 1e-6:
        print("✓ Equity curve 완전 일치")
    elif rel_diff < 0.001:
        print(f"△ 근사 일치 (상대오차 {rel_diff*100:.6f}% < 0.1%)")
    else:
        print(f"✗ Equity curve 오차 큼 ({rel_diff*100:.4f}%)")
        diffs = np.abs(bt_equity[:min_len] - live_equity[:min_len]) / denom
        worst = int(np.argmax(diffs))
        wdate = (
            df_full.index[worst].date().isoformat()
            if hasattr(df_full.index[worst], "date")
            else str(df_full.index[worst])[:10]
        )
        print(f"  최대 오차 날짜: {wdate}  bt={bt_equity[worst]:.6f}  live={live_equity[worst]:.6f}")


if __name__ == "__main__":
    main()
