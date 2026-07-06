"""drop_buy: no zero-share lots when cash is insufficient."""

from __future__ import annotations

from bear_bull_drop_buy.drop_buy import Lot, buy_from_cash


def test_buy_from_cash_no_append_when_cash_zero():
    lots: list[Lot] = []
    cash, lots, lot = buy_from_cash(0.0, lots, price=10.0, spend=100.0, buy_mult=1.001)
    assert cash == 0.0
    assert lots == []
    assert lot.shares == 0.0


def test_buy_from_cash_appends_when_cash_available():
    lots: list[Lot] = []
    cash, lots, lot = buy_from_cash(50.0, lots, price=10.0, spend=30.0, buy_mult=1.001)
    assert len(lots) == 1
    assert lot.shares > 0
    assert cash < 50.0
