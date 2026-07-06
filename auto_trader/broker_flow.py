"""한국투자 해외주식 OHLCV·시세·주문 헬퍼 (usa_auto_trade_du_wc 참고)."""

from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import timedelta
from typing import Any, Callable, Optional

import pandas as pd

import koreainvestment as mojito

log = logging.getLogger(__name__)


def ohlcv_rows_to_dataframe(ohlcv_data: list) -> Optional[pd.DataFrame]:
    df = pd.DataFrame(ohlcv_data)
    if "xymd" not in df.columns:
        return None
    dt = pd.to_datetime(df["xymd"], format="%Y%m%d")
    df = df.set_index(dt)
    df = df[["xymd", "open", "high", "low", "clos", "tvol"]]
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df[["open", "high", "low", "close", "volume"]] = df[
        ["open", "high", "low", "close", "volume"]
    ].astype(float)
    df.index.name = "index"
    return df


def fetch_ohlcv_paged(
    broker: mojito.KoreaInvestment,
    symbol: str,
    *,
    exchange: str,
    need_length: int = 400,
    adj_price: bool = True,
    retry_cnt: int = 5,
) -> Optional[pd.DataFrame]:
    api_max_len = 100
    dfs: list[pd.DataFrame] = []
    end_day = ""
    resp = broker.fetch_ohlcv(symbol=symbol, timeframe="D", end_day=end_day, adj_price=adj_price, exchange=exchange)
    cnt = 0
    while "output2" not in resp and cnt < retry_cnt:
        if resp is not None and "rt_cd" in resp and resp["rt_cd"] == "1":
            time.sleep(0.05)
        resp = broker.fetch_ohlcv(
            symbol=symbol, timeframe="D", end_day=end_day, adj_price=adj_price, exchange=exchange
        )
        cnt += 1
    if "output2" not in resp:
        return None
    ohlcv_data = resp["output2"]
    if len(ohlcv_data) < api_max_len:
        return None
    df = ohlcv_rows_to_dataframe(ohlcv_data)
    if df is None:
        return None
    dfs.append(df)

    loop_cnt = int((need_length - 1) / 100)
    for loop_idx in range(loop_cnt):
        last_day = pd.to_datetime(df.index[api_max_len - 1])
        end_day = (last_day - timedelta(days=1)).strftime("%Y%m%d")
        resp = broker.fetch_ohlcv(symbol=symbol, timeframe="D", end_day=end_day, adj_price=adj_price, exchange=exchange)
        cnt = 0
        while resp.get("rt_cd") != "0" and cnt < retry_cnt:
            if resp is not None and "rt_cd" in resp and resp["rt_cd"] == "1":
                time.sleep(0.05)
            resp = broker.fetch_ohlcv(
                symbol=symbol, timeframe="D", end_day=end_day, adj_price=adj_price, exchange=exchange
            )
            cnt += 1
        if resp.get("rt_cd") != "0":
            return None
        ohlcv_data = resp["output2"]
        if loop_idx < (loop_cnt - 1):
            if len(ohlcv_data) < api_max_len:
                return None
        else:
            if len(ohlcv_data) < (need_length % 100):
                return None
        df = ohlcv_rows_to_dataframe(ohlcv_data)
        if df is None:
            return None
        dfs.append(df)

    result_df = pd.concat(dfs)
    result_df = result_df.sort_index(ascending=True).drop_duplicates()
    result_df = result_df.reset_index(drop=True)
    return result_df


def is_price_resp_ok(resp: dict) -> bool:
    if resp.get("rt_cd") != "0":
        return False
    out = resp.get("output")
    if not isinstance(out, dict):
        return False
    if "last" not in out or "e_hogau" not in out:
        return False
    return True


def _highest_ask(book: dict) -> float:
    """호가창에서 가장 높은 매도호가(pask10→pask1 순) 반환. 없으면 0.0.

    매수 주문가격으로 사용 — 모든 매도호가보다 높으므로 즉시 체결 보장.
    """
    for i in range(10, 0, -1):
        p = float(book.get(f"pask{i}", 0) or 0)
        if p > 0:
            return p
    return 0.0


def _lowest_bid(book: dict) -> float:
    """호가창에서 가장 낮은 매수호가(pbid10→pbid1 순) 반환. 없으면 0.0.

    매도 주문가격으로 사용 — 모든 매수호가보다 낮으므로 즉시 체결 보장.
    """
    for i in range(10, 0, -1):
        p = float(book.get(f"pbid{i}", 0) or 0)
        if p > 0:
            return p
    return 0.0


def market_sell_limit(
    broker: mojito.KoreaInvestment,
    code: str,
    quantity: int,
    *,
    exchange: str,
    retry_cnt: int = 5,
    sleep_time: float = 0.02,
    refresh_broker_fn: Optional[Callable[[dict[str, Any]], mojito.KoreaInvestment]] = None,
) -> tuple[int, dict[str, Any]]:
    """지정가 매도 주문 (호가창 최우선 매수호가 pbid1).

    반환: (주문수량, 응답dict) — 실패 시 (0, resp)
    """
    q = int(quantity)
    if q <= 0:
        return 0, {}
    book_resp = broker.fetch_oversea_order_book(code, exchange)
    book = (book_resp.get("output2") or {})
    bid_price = _lowest_bid(book)
    if bid_price <= 0:
        price_resp = broker.fetch_price(code, exchange)
        if is_price_resp_ok(price_resp):
            last = float(price_resp["output"]["last"])
            delta = float(price_resp["output"].get("e_hogau", 0.01) or 0.01)
            bid_price = round(last - 50 * delta, 2)
            log.warning("market_sell_limit[%s]: 호가창 실패 → 현재가 fallback  bid=%.4f", code, bid_price)
        else:
            log.warning("market_sell_limit[%s]: 호가창·현재가 모두 실패  book_rt=%s  price_rt=%s",
                        code, book_resp.get("rt_cd"), price_resp.get("rt_cd"))
            return 0, {"rt_cd": "ERR", "msg1": "호가창 매수호가 없음"}
    order_resp = broker.create_limit_sell_order(symbol=code, price=bid_price, quantity=q, exchange=exchange)
    cnt = 0
    token_refreshed = False
    while order_resp.get("rt_cd") != "0" and cnt < retry_cnt:
        if order_resp.get("rt_cd") == "1" and refresh_broker_fn is not None and not token_refreshed:
            log.warning("market_sell_limit[%s]: 토큰 만료 → broker 재발급 후 재시도", code)
            broker = refresh_broker_fn(order_resp)
            token_refreshed = True
            order_resp = broker.create_limit_sell_order(symbol=code, price=bid_price, quantity=q, exchange=exchange)
            continue
        time.sleep(sleep_time)
        price_resp = broker.fetch_price(code, exchange)
        if is_price_resp_ok(price_resp):
            last = float(price_resp["output"]["last"])
            delta = float(price_resp["output"].get("e_hogau", 0.01) or 0.01)
            bid_price = round(last - 50 * delta, 2)
            if bid_price <= 0:
                return 0, order_resp
        order_resp = broker.create_limit_sell_order(symbol=code, price=bid_price, quantity=q, exchange=exchange)
        cnt += 1
    if order_resp.get("rt_cd") == "0":
        return q, order_resp
    return 0, order_resp


def market_buy_limit(
    broker: mojito.KoreaInvestment,
    code: str,
    can_alloc_cash: float,
    *,
    exchange: str,
    retry_cnt: int = 5,
    sleep_time: float = 0.02,
    refresh_broker_fn: Optional[Callable[[dict[str, Any]], mojito.KoreaInvestment]] = None,
) -> tuple[int, dict[str, Any]]:
    """지정가 매수 주문 (호가창 최우선 매도호가 pask1).

    수량 = int(can_alloc_cash / ask_price), 최소 1주.
    반환: (주문수량, 응답dict) — 실패 시 (0, resp)
    """
    book_resp = broker.fetch_oversea_order_book(code, exchange)
    book = (book_resp.get("output2") or {})
    ask_price = _highest_ask(book)
    ob_cnt = 0
    while ask_price <= 0 and ob_cnt < retry_cnt:
        log.warning("market_buy_limit[%s]: 호가창 응답 이상 (attempt %d)  rt=%s  book=%s",
                    code, ob_cnt + 1, book_resp.get("rt_cd"), book)
        time.sleep(sleep_time * 10)
        book_resp = broker.fetch_oversea_order_book(code, exchange)
        book = (book_resp.get("output2") or {})
        ask_price = _highest_ask(book)
        ob_cnt += 1
    if ask_price <= 0:
        price_resp = broker.fetch_price(code, exchange)
        if is_price_resp_ok(price_resp):
            last = float(price_resp["output"]["last"])
            delta = float(price_resp["output"].get("e_hogau", 0.01) or 0.01)
            ask_price = round(last + 50 * delta, 2)
            log.warning("market_buy_limit[%s]: 호가창 실패 → 현재가 fallback  ask=%.4f  book_rt=%s",
                        code, ask_price, book_resp.get("rt_cd"))
        else:
            log.warning("market_buy_limit[%s]: 호가창·현재가 모두 실패  book_rt=%s  price_rt=%s  book=%s",
                        code, book_resp.get("rt_cd"), price_resp.get("rt_cd"), book)
            return 0, {"rt_cd": "ERR", "msg1": "호가창 매도호가 없음"}
    quantity = int(can_alloc_cash / ask_price)
    if quantity <= 0:
        return 0, {"rt_cd": "ERR", "msg1": "수량 0 (현금 부족)"}
    order_resp = broker.create_limit_buy_order(symbol=code, price=ask_price, quantity=quantity, exchange=exchange)
    cnt = 0
    token_refreshed = False
    while order_resp.get("rt_cd") != "0" and cnt < retry_cnt:
        if order_resp.get("rt_cd") == "1" and refresh_broker_fn is not None and not token_refreshed:
            log.warning("market_buy_limit[%s]: 토큰 만료 → broker 재발급 후 재시도", code)
            broker = refresh_broker_fn(order_resp)
            token_refreshed = True
            order_resp = broker.create_limit_buy_order(symbol=code, price=ask_price, quantity=quantity, exchange=exchange)
            continue
        if order_resp.get("rt_cd") == "7" and order_resp.get("msg_cd") == "APBK0952":
            quantity = int(quantity * 0.99)
            if quantity <= 0:
                return 0, order_resp
        time.sleep(sleep_time)
        price_resp = broker.fetch_price(code, exchange)
        if is_price_resp_ok(price_resp):
            last = float(price_resp["output"]["last"])
            delta = float(price_resp["output"].get("e_hogau", 0.01) or 0.01)
            ask_price = round(last + 50 * delta, 2)
            quantity = int(can_alloc_cash / ask_price)
            if quantity <= 0:
                return 0, order_resp
        order_resp = broker.create_limit_buy_order(symbol=code, price=ask_price, quantity=quantity, exchange=exchange)
        cnt += 1
    if order_resp.get("rt_cd") == "0":
        return quantity, order_resp
    return 0, order_resp


def flush_pending_orders(
    broker: mojito.KoreaInvestment,
    symbol: str,
    *,
    exchange: str,
    retry_cnt: int = 3,
    sleep_after: float = 1.5,
) -> int:
    """미체결 주문을 강제 체결 가능 가격으로 정정.

    매수 미체결: 호가창 최고 매도호가(pask10)로 정정
                 원래 주문금액(가격×수량) 유지 → 높아진 가격만큼 수량 재계산
    매도 미체결: 호가창 최저 매수호가(pbid10)로 정정 (수량 유지)
    반환: 정정 성공 건수
    """
    resp = broker.fetch_oversea_open_orders(symbol=symbol, exchange=exchange)
    orders = [o for o in (resp.get("output") or []) if o.get("pdno") == symbol]
    if not orders:
        return 0

    book = (broker.fetch_oversea_order_book(symbol, exchange).get("output2") or {})

    price_resp = broker.fetch_price(symbol, exchange)
    cur_price, delta = 0.0, 0.01
    if is_price_resp_ok(price_resp):
        cur_price = float(price_resp["output"]["last"])
        delta = float(price_resp["output"].get("e_hogau", 0.01) or 0.01)

    processed = 0
    for o in orders:
        qty = int(o.get("nccs_qty", 0) or 0)
        if qty <= 0:
            continue
        is_buy = o.get("sll_buy_dvsn_cd", "") == "02"
        order_no = o.get("odno", "")

        if is_buy:
            orig_price = float(o.get("ovrs_ord_unpr", 0) or 0)
            orig_notional = orig_price * qty if orig_price > 0 else 0.0

            ask_prices = [float(book.get(f"pask{i}", 0) or 0) for i in range(10, 0, -1)]
            valid = [p for p in ask_prices if p > 0]
            target_price = round(valid[0] if valid else cur_price + 50 * delta, 2)
            if target_price <= 0:
                continue

            new_qty = int(orig_notional / target_price) if orig_notional > 0 else qty
            if new_qty <= 0:
                continue
        else:
            bid_prices = [float(book.get(f"pbid{i}", 0) or 0) for i in range(10, 0, -1)]
            valid = [p for p in bid_prices if p > 0]
            target_price = round(valid[0] if valid else cur_price - 50 * delta, 2)
            if target_price <= 0:
                continue
            new_qty = qty

        for _ in range(retry_cnt):
            r = broker.modify_oversea_order(order_no, symbol, new_qty, target_price, exchange)
            if r.get("rt_cd") == "0":
                processed += 1
                break
            time.sleep(0.1)

    if processed > 0:
        time.sleep(sleep_after)
    return processed


def cancel_all_open_orders(
    broker: mojito.KoreaInvestment,
    symbol: str,
    *,
    exchange: str,
    retry_cnt: int = 3,
) -> int:
    """symbol의 모든 미체결 해외주식 주문을 취소. 취소 성공 건수를 반환."""
    cancelled = 0
    ctx_fk, ctx_nk = "", ""
    while True:
        resp = broker.fetch_oversea_open_orders(
            symbol=symbol, ctx_area_fk200=ctx_fk, ctx_area_nk200=ctx_nk, exchange=exchange
        )
        orders = resp.get("output") or []
        if not orders:
            break
        found_any = False
        for o in orders:
            if o.get("pdno", "") != symbol:
                continue
            qty = int(o.get("nccs_qty", 0) or 0)
            if qty <= 0:
                continue
            found_any = True
            order_no = o.get("odno", "")
            for _ in range(retry_cnt):
                r = broker.cancel_oversea_order(order_no, symbol, qty, exchange)
                if r.get("rt_cd") == "0":
                    cancelled += 1
                    break
                time.sleep(0.1)
        if not found_any:
            break
        tr_cont = resp.get("tr_cont", "")
        if tr_cont not in ("F", "M"):
            break
        ctx_fk = resp.get("ctx_area_fk200", "")
        ctx_nk = resp.get("ctx_area_nk200", "")
    return cancelled


RefreshBrokerFn = Callable[[dict[str, Any], Any, Optional[dict[str, Any]]], None]

TickerInfo = dict[str, float | str]


def get_balance_info(
    broker_box: list,
    *,
    cfg: dict[str, Any],
    retry_cnt: int,
    refresh_broker_fn: Optional[RefreshBrokerFn] = None,
    per_exchange_response: Optional[dict[str, Any]] = None,
) -> tuple[float, float, dict[str, TickerInfo]]:
    """단일 broker로 미국 전 거래소 체결기준현재잔고 조회.

    fetch_present_balance(exchange="미국전체")를 한 번 호출해
    NASDAQ·NYSE·AMEX 포지션을 동시에 조회한다.
    """
    import time

    last_exc: Optional[Exception] = None
    n = 0
    while n < retry_cnt:
        try:
            broker = broker_box[0]
            if broker is None:
                raise RuntimeError("broker not initialized")

            resp = broker.fetch_present_balance(exchange="미국전체")
            if resp is not None and resp.get("rt_cd") == "1" and refresh_broker_fn is not None:
                refresh_broker_fn(cfg, broker_box, resp)
                broker = broker_box[0]
                if broker is None:
                    raise RuntimeError("broker None after refresh")
                resp = broker.fetch_present_balance(exchange="미국전체")

            if "output2" not in resp:
                raise RuntimeError("fetch_present_balance 실패: output2 없음")

            cash = 0.0
            for info in resp["output2"]:
                if info.get("crcy_cd") == "USD":
                    dncl = float(info.get("frcr_dncl_amt_2", 0) or 0)
                    buy = float(info.get("frcr_buy_amt_smtl", 0) or 0)
                    sll = float(info.get("frcr_sll_amt_smtl", 0) or 0)
                    cash = dncl - buy + sll
                    break

            total_eval_amount = cash
            code_info: dict[str, TickerInfo] = {}
            for hold_stock_info in resp.get("output1") or []:
                code = str(hold_stock_info.get("pdno", ""))
                name = str(hold_stock_info.get("prdt_name", ""))
                quantity = float(hold_stock_info.get("ccld_qty_smtl1", 0) or 0)
                buy_amount = float(hold_stock_info.get("frcr_pchs_amt", 0) or 0)
                cur_amount = float(hold_stock_info.get("frcr_evlu_amt2", 0) or 0)
                row: TickerInfo = {
                    "name": name,
                    "quantity": quantity,
                    "buy_amount": buy_amount,
                    "cur_amount": cur_amount,
                }
                if quantity > 0:
                    code_info[code] = row
                total_eval_amount += cur_amount

            if per_exchange_response is not None:
                per_exchange_response["미국전체"] = resp

            block = {
                "exchange": "미국전체",
                "rt_cd": resp.get("rt_cd"),
                "msg_cd": resp.get("msg_cd"),
                "msg1": resp.get("msg1"),
                "output1": resp.get("output1"),
                "output2": resp.get("output2"),
            }
            log.info("=== fetch_present_balance 원본 (미국전체) ===\n%s",
                     json.dumps(block, ensure_ascii=False, indent=2, default=str))
            log.info(
                "=== get_balance_info() 결과 ===\n"
                "total_eval_amount = %.4f\n"
                "cash (USD frcr_dncl_amt_2 - frcr_buy_amt_smtl + frcr_sll_amt_smtl) = %.4f\n"
                "hold_stocks_info:\n%s",
                total_eval_amount,
                cash,
                json.dumps(code_info, ensure_ascii=False, indent=2, default=str),
            )
            return total_eval_amount, cash, code_info

        except Exception as e:
            last_exc = e
            if refresh_broker_fn is not None:
                refresh_broker_fn(cfg, broker_box, None)
            n += 1
            time.sleep(20 * max(n, 1))

    log.error("get_balance_info FAILED: %s", last_exc)
    raise RuntimeError("get_balance_info failed") from last_exc


def log_exc() -> str:
    return traceback.format_exc()
