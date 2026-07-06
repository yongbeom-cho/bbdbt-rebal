"""Single cash pool + marked lots."""

from __future__ import annotations

from bear_bull_drop_buy.drop_buy import Lot, mark_lots_value


def total_equity(cash: float, lots: list[Lot], price: float) -> float:
    return float(cash) + mark_lots_value(lots, price)


def drop_buy_sizing_equity(cash: float, lots: list[Lot], price: float) -> float:
    """Total equity; base for eq_buy_frac * sizing."""
    return max(0.0, total_equity(cash, lots, price))
