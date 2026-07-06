"""Drop-side lots and execution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class Lot:
    shares: float
    entry: float


def mark_lots_value(lots: List[Lot], price: float) -> float:
    return sum(l.shares for l in lots) * price


_SHARE_EPS = 1e-12


def buy_from_cash(
    cash: float,
    lots: List[Lot],
    price: float,
    spend: float,
    buy_mult: float,
) -> Tuple[float, List[Lot], Lot]:
    if spend <= 0 or price <= 0 or cash <= _SHARE_EPS:
        return cash, lots, Lot(0.0, price)
    eff = price * buy_mult
    if eff <= 0:
        return cash, lots, Lot(0.0, price)
    spend = min(spend, cash)
    shares = spend / eff
    if shares <= _SHARE_EPS:
        return cash, lots, Lot(0.0, price)
    cash -= spend
    lot = Lot(shares=shares, entry=price)
    lots.append(lot)
    return cash, lots, lot


def sell_lot(
    cash: float,
    lots: List[Lot],
    idx: int,
    price: float,
    sell_mult: float,
) -> Tuple[float, List[Lot]]:
    lot = lots[idx]
    cash += lot.shares * price * sell_mult
    lots = lots[:idx] + lots[idx + 1 :]
    return cash, lots
