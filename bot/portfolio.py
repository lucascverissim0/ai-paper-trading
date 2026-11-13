"""Turns the AI's wishes into safe orders and stop-losses. Pure functions, no network."""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import config


@dataclass
class PlannedOrder:
    symbol: str
    side: str          # "buy" or "sell"
    qty: int           # whole shares; ignored when close_all is True
    close_all: bool = False


def clean_weights(raw: dict) -> dict[str, float]:
    """No negatives or bad numbers, total at most 1 (the rest is cash). No cap per stock (Lucas's choice)."""
    weights: dict[str, float] = {}
    for symbol, value in (raw or {}).items():
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            weights[symbol] = value
    total = sum(weights.values())
    if total > 1.0:
        weights = {s: v / total for s, v in weights.items()}
    return weights


def plan_orders(target: dict[str, float], held: dict[str, dict], equity: float,
                prices: dict[str, float]) -> list[PlannedOrder]:
    """Sells first (to free cash), then buys, in whole shares. Small changes are skipped to save fees.

    `held` is symbol -> {"qty": shares, "value": dollars}.
    """
    threshold = config.MIN_TRADE_FRACTION * equity
    sells: list[PlannedOrder] = []
    buys: list[PlannedOrder] = []
    for symbol, pos in held.items():
        if pos["qty"] > 0 and target.get(symbol, 0.0) <= 0:
            sells.append(PlannedOrder(symbol, "sell", 0, close_all=True))
    for symbol, weight in target.items():
        price = prices.get(symbol)
        if weight <= 0 or not price or price <= 0:
            continue
        pos = held.get(symbol, {"qty": 0.0, "value": 0.0})
        diff = weight * equity - pos["value"]
        if abs(diff) < max(threshold, price):
            continue
        qty = int(abs(diff) // price)
        if diff < 0:
            if qty >= math.floor(pos["qty"]):
                sells.append(PlannedOrder(symbol, "sell", 0, close_all=True))
            elif qty > 0:
                sells.append(PlannedOrder(symbol, "sell", qty))
        elif qty > 0:
            buys.append(PlannedOrder(symbol, "buy", qty))
    return sells + buys


def fit_buys_to_cash(buys: list[PlannedOrder], cash: float, prices: dict[str, float]) -> list[PlannedOrder]:
    """Shrink buys proportionally so they never exceed available cash (no borrowing)."""
    available = max(cash, 0.0) * 0.99  # room for the price moving before the fill
    total = sum(o.qty * prices[o.symbol] for o in buys)
    if total <= available or total == 0:
        return buys
    scale = available / total
    fitted = [PlannedOrder(o.symbol, o.side, int(math.floor(o.qty * scale))) for o in buys]
    return [o for o in fitted if o.qty >= 1]


def choose_stop(proposed: float | None, price: float, old: float | None) -> float:
    """The stop-loss to keep: the AI's if usable, else 8% below the price; never lower than before."""
    low, high = price * (1 - config.MAX_STOP_GAP), price * (1 - config.MIN_STOP_GAP)
    if proposed is None or not math.isfinite(proposed) or proposed >= high or proposed <= 0:
        stop = price * (1 - config.DEFAULT_STOP)
    else:
        stop = max(proposed, low)
    if old is not None and old > stop:
        stop = old
    return round(stop, 2)
