"""Strategy parameters (JSON-serializable).

Grid JSON is one flat object: each key is a StrategyParams field; each value
is either a list or {"min","max","step"} for the Cartesian product. Legacy
{"defaults","axes"} is still merged into one flat map.

commission and slippage are never swept: only a single scalar is allowed if
present; omit them to use defaults from StrategyParams.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class StrategyParams:
    # regime (bull/bear): subsampled MA slope on close every d_interval bars
    regime_ma_type: str = "wma"  # "sma" | "wma" | "ema"
    d_interval: int = 5
    period: int = 4

    # drop-buy (regime-split)
    bear_take_profit_pct: float = 0.10
    bull_take_profit_pct: float = 0.11
    bear_day_drop_buy_pct: float = 0.096
    bull_day_drop_buy_pct: float = 0.005
    bear_equity_buy_frac: float = 0.35
    bull_equity_buy_frac: float = 0.35
    bear_day_surge_partial_exit_pct: float = 0.07
    bull_day_surge_partial_exit_pct: float = 0.03
    bear_day_surge_sell_newest_n: int = 2
    bull_day_surge_sell_newest_n: int = 2

    # costs
    commission: float = 0.0025
    slippage: float = 0.001

    def validate(self) -> None:
        if self.d_interval < 1 or self.period < 1:
            raise ValueError("d_interval and period must be >= 1")
        if self.regime_ma_type.lower().strip() not in ("sma", "wma", "ema"):
            raise ValueError('regime_ma_type must be "sma", "wma", or "ema"')

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(m: dict[str, Any]) -> "StrategyParams":
        raw = dict(m)
        base = asdict(StrategyParams())
        base.update(raw)
        # Legacy auto_trader configs use wma_period; defaults already set period=4.
        if "period" not in raw and "wma_period" in raw:
            base["period"] = raw["wma_period"]
        d = base
        return StrategyParams(
            regime_ma_type=str(d.get("regime_ma_type", "wma")).lower().strip(),
            d_interval=int(d["d_interval"]),
            period=int(d.get("period", d.get("wma_period", 4))),
            bear_take_profit_pct=float(d["bear_take_profit_pct"]),
            bull_take_profit_pct=float(d["bull_take_profit_pct"]),
            bear_day_drop_buy_pct=float(d["bear_day_drop_buy_pct"]),
            bull_day_drop_buy_pct=float(d["bull_day_drop_buy_pct"]),
            bear_equity_buy_frac=float(d["bear_equity_buy_frac"]),
            bull_equity_buy_frac=float(d["bull_equity_buy_frac"]),
            bear_day_surge_partial_exit_pct=float(d["bear_day_surge_partial_exit_pct"]),
            bull_day_surge_partial_exit_pct=float(d["bull_day_surge_partial_exit_pct"]),
            bear_day_surge_sell_newest_n=int(d["bear_day_surge_sell_newest_n"]),
            bull_day_surge_sell_newest_n=int(d["bull_day_surge_sell_newest_n"]),
            commission=float(d.get("commission", 0.0025)),
            slippage=float(d.get("slippage", 0.0002)),
        )


def max_dip_rungs(equity_buy_frac: float, cap: int = 200) -> int:
    if equity_buy_frac <= 0:
        return 0
    if equity_buy_frac >= 1.0:
        return 1
    return max(1, min(cap, int(1.0 / equity_buy_frac)))
