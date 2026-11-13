"""Turns the AI's wishes into safe orders. Pure functions, no network."""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import config


@dataclass
class PlannedOrder:
    symbol: str
    side: str          # "buy" or "sell"
    notional: float    # dollars; ignored when close_all is True
    close_all: bool = False


def clean_weights(raw: dict) -> dict[str, float]:
    """Keep only allowed ETFs, no negatives, each at most MAX_WEIGHT, total at most 1 (rest is cash)."""
    weights: dict[str, float] = {}
    for symbol in config.UNIVERSE:
        try:
            value = float(raw.get(symbol, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if not math.isfinite(value) or value < 0:
            value = 0.0
        weights[symbol] = min(value, config.MAX_WEIGHT)
    total = sum(weights.values())
    if total > 1.0:
        weights = {s: v / total for s, v in weights.items()}
    return weights


def plan_orders(target: dict[str, float], positions: dict[str, float], equity: float) -> list[PlannedOrder]:
    """Sells first (to free cash), then buys. Small changes are skipped to save fees."""
    threshold = config.MIN_TRADE_FRACTION * equity
    sells: list[PlannedOrder] = []
    buys: list[PlannedOrder] = []

    for symbol, value in positions.items():
        if symbol not in config.UNIVERSE and value != 0:
            sells.append(PlannedOrder(symbol, "sell", 0.0, close_all=True))

    for symbol in config.UNIVERSE:
        wanted = target.get(symbol, 0.0) * equity
        held = positions.get(symbol, 0.0)
        if wanted == 0 and held > 0:
            sells.append(PlannedOrder(symbol, "sell", 0.0, close_all=True))
            continue
        diff = wanted - held
        if abs(diff) < threshold or abs(diff) < 1.0:
            continue
        if diff < 0:
            sells.append(PlannedOrder(symbol, "sell", round(-diff, 2)))
        else:
            buys.append(PlannedOrder(symbol, "buy", round(diff, 2)))
    return sells + buys


def fit_buys_to_cash(buys: list[PlannedOrder], cash: float) -> list[PlannedOrder]:
    """Shrink buys proportionally so they never exceed available cash (no borrowing)."""
    available = max(cash, 0.0) * 0.995
    total = sum(o.notional for o in buys)
    if total <= available or total == 0:
        return buys
    scale = available / total
    fitted = [PlannedOrder(o.symbol, o.side, math.floor(o.notional * scale * 100) / 100) for o in buys]
    return [o for o in fitted if o.notional >= 1.0]
