"""Decides whether Claude should be woken up today. Pure functions, no network, no cost."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from . import config

AI_STATUSES = {"ok", "refused", "truncated", "bad_output", "api_error", "no_consensus"}


def trading_days_after(start: str, end: str) -> int:
    """Weekdays strictly after `start` up to and including `end` (holidays are not removed)."""
    d, stop, n = date.fromisoformat(start), date.fromisoformat(end), 0
    while d < stop:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def add_trading_days(start: str, n: int) -> str:
    d = date.fromisoformat(start)
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.isoformat()


def clamp_review_days(value) -> int:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        n = config.MIN_REVIEW_DAYS
    return max(config.MIN_REVIEW_DAYS, min(config.MAX_REVIEW_DAYS, n))


def last_decision(state: dict) -> Optional[dict]:
    return next((d for d in reversed(state["days"]) if d.get("decision_status") == "ok"), None)


def next_review_date(state: dict) -> Optional[str]:
    last = last_decision(state)
    if not last:
        return None
    return add_trading_days(last["date"], clamp_review_days(last.get("next_review_days")))


def _close_before(series: list[tuple[str, float]], day: str) -> Optional[float]:
    before = [c for d, c in series if d < day]
    return before[-1] if before else None


def why_decide(state: dict, today: str, closes: dict, equity: float) -> Optional[str]:
    """A plain-words reason to call Claude today, or None to wait (free day)."""
    attempts = [d for d in state["days"] if d.get("decision_status") in AI_STATUSES]
    last = last_decision(state)
    if not attempts:
        return "first decision of the test"
    if attempts[-1].get("decision_status") != "ok":
        return "retry after an unusable answer on " + attempts[-1]["date"]
    if last is None:
        return "no valid decision yet"

    due = clamp_review_days(last.get("next_review_days"))
    if trading_days_after(last["date"], today) >= due:
        return f"scheduled review ({due} trading day(s) after {last['date']})"

    spy = closes.get(config.BENCHMARK, [])
    if len(spy) >= 2 and spy[-1][0] >= last["date"]:
        move = spy[-1][1] / spy[-2][1] - 1
        if abs(move) > config.JOLT_SPY_DAY:
            return f"SPY moved {move * 100:+.1f}% on {spy[-1][0]}"

    for symbol, weight in (last.get("weights") or {}).items():
        series = closes.get(symbol, [])
        then = _close_before(series, last["date"])
        if weight > 0.01 and then and series:
            move = series[-1][1] / then - 1
            if abs(move) > config.JOLT_HELD_ETF:
                return f"{symbol} moved {move * 100:+.1f}% since the last decision"

    if last.get("equity") and equity < last["equity"] * (1 - config.JOLT_PORTFOLIO):
        return f"portfolio fell {(1 - equity / last['equity']) * 100:.1f}% since the last decision"
    return None
