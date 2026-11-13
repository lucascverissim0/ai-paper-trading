"""Simple rule-based portfolios run on the same prices, dates and fees as the AI, at no API cost.

They answer: does Claude add anything beyond rules anyone could follow?
Prices are the ones recorded at each run (no dividends, like the AI and the SPY benchmark).
"""
from __future__ import annotations

from . import config

SHADOWS = {
    "s_6040": "60% SPY / 40% IEF",
    "s_equal": "Equal weight, 8 ETFs",
    "s_trend": "Trend rule (above 200-day average)",
}


def _fee(amount: float) -> float:
    return config.FEE_PER_ORDER_USD + config.FEE_RATE * amount if amount > 0 else 0.0


def _targets(name: str, day: dict) -> dict[str, float]:
    if name == "s_6040":
        return {"SPY": 0.6, "IEF": 0.4}
    if name == "s_equal":
        return {s: 1 / len(config.UNIVERSE) for s in config.UNIVERSE}
    up = [s for s in day.get("trend_up", []) if s in config.UNIVERSE]
    return {s: 1 / len(up) for s in up}


def _rebalance_due(name: str, day: dict, prev_day: dict | None, prev_targets: dict | None) -> bool:
    if prev_day is None:
        return True
    if name == "s_trend":
        return set(_targets(name, day)) != set(prev_targets or {})
    return day["date"][:7] != prev_day["date"][:7]  # first run of a new month


def simulate(days: list[dict], name: str) -> list[dict]:
    """[{date, value}] at real capital, after modelled fees. Days without recorded prices are skipped."""
    cash = config.REAL_CAPITAL_USD
    shares: dict[str, float] = {}
    out: list[dict] = []
    prev_day, prev_targets = None, None
    for day in days:
        prices = day.get("prices")
        if not prices or any(s not in prices for s in config.UNIVERSE):
            continue
        value = cash + sum(q * prices[s] for s, q in shares.items())
        if _rebalance_due(name, day, prev_day, prev_targets):
            targets = _targets(name, day)
            for s in sorted(set(shares) | set(targets), key=lambda s: targets.get(s, 0)):  # sells first
                held = shares.get(s, 0.0) * prices[s]
                diff = targets.get(s, 0.0) * value - held
                selling_all = targets.get(s, 0.0) == 0 and held > 0
                if not selling_all and abs(diff) < max(config.MIN_TRADE_FRACTION * value, 1.0):
                    continue
                amount = held if selling_all else abs(diff)
                if diff > 0:
                    amount = min(amount, max(cash - _fee(amount), 0.0))
                    if amount <= 0:
                        continue
                    cash -= amount + _fee(amount)
                    shares[s] = shares.get(s, 0.0) + amount / prices[s]
                else:
                    cash += amount - _fee(amount)
                    shares[s] = shares.get(s, 0.0) - amount / prices[s]
                    if selling_all or shares[s] < 1e-12:
                        shares.pop(s, None)
            prev_targets = targets
            value = cash + sum(q * prices[s] for s, q in shares.items())
        prev_day = day
        out.append({"date": day["date"], "value": round(value, 2)})
    return out


def all_shadows(days: list[dict]) -> dict[str, list[dict]]:
    return {name: simulate(days, name) for name in SHADOWS}
