"""The daily stock screen: every tradable US-listed share, filtered and ranked by code. Free, no AI.

Method: the "trend template" used by momentum growth-stock traders (Mark Minervini, US Investing
Champion 1997 and 2021) and the relative-strength ranking of William O'Neil's CAN SLIM. Only stocks
already in a strong, orderly uptrend and stronger than most of the market reach the AI.
Pure functions here; the price download lives in alpaca.py.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import config

Bars = list[tuple[str, float, float]]  # (YYYY-MM-DD, close, volume), oldest first


def tradable_assets(raw: list[dict]) -> dict[str, str]:
    """Symbol -> company name for every active, tradable share on a main US exchange."""
    out = {}
    for a in raw:
        symbol = str(a.get("symbol") or "")
        if (a.get("status") == "active" and a.get("tradable") and a.get("exchange") in config.EXCHANGES
                and symbol and "/" not in symbol):
            out[symbol] = str(a.get("name") or "")
    return out


def is_leveraged(name: str) -> bool:
    lower = f" {name.lower()} "
    return any(word in lower for word in config.LEVERAGED_WORDS)


def _ret(closes: list[float], n: int) -> float | None:
    return closes[-1] / closes[-1 - n] - 1 if len(closes) > n and closes[-1 - n] > 0 else None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


@dataclass
class Stats:
    symbol: str
    price: float
    dollar_volume: float      # average per day, 50 days
    rs_score: float           # weighted 3/6/9/12-month return, the most recent quarter counting double
    r1m: float | None
    r3m: float | None
    r6m: float | None
    r12m: float | None
    ma50: float
    ma150: float
    ma200: float
    ma200_month_ago: float
    high52: float
    low52: float
    volume_surge: float       # last 5 days' volume / 50-day average
    tightness: float          # range of the last 10 closes as a share of the price (small = tight base)
    closes: list[float] = field(default_factory=list)
    rs_percentile: float = 0.0
    passes: bool = False


def stats_for(symbol: str, bars: Bars) -> Stats | None:
    if len(bars) < config.SCREEN_MIN_BARS:
        return None
    closes = [c for _, c, _ in bars]
    volumes = [v for _, _, v in bars]
    last = closes[-1]
    if last <= 0:
        return None
    year = closes[-252:]
    v50 = _mean(volumes[-50:])
    parts = [(_ret(closes, 63), 2), (_ret(closes, 126), 1), (_ret(closes, 189), 1), (_ret(closes, 252), 1)]
    known = [(r, w) for r, w in parts if r is not None]
    rs = sum(r * w for r, w in known) / sum(w for _, w in known)
    return Stats(
        symbol=symbol,
        price=last,
        dollar_volume=_mean([c * v for c, v in zip(closes[-50:], volumes[-50:])]),
        rs_score=rs,
        r1m=_ret(closes, 21), r3m=_ret(closes, 63), r6m=_ret(closes, 126), r12m=_ret(closes, 252),
        ma50=_mean(closes[-50:]), ma150=_mean(closes[-150:]), ma200=_mean(closes[-200:]),
        ma200_month_ago=_mean(closes[-221:-21]),
        high52=max(year), low52=min(year),
        volume_surge=_mean(volumes[-5:]) / v50 if v50 > 0 else 0.0,
        tightness=(max(closes[-10:]) - min(closes[-10:])) / last,
        closes=closes[-config.PROMPT_CLOSES:],
    )


def trend_template(s: Stats) -> bool:
    """Minervini's trend template: an established, orderly uptrend near its highs."""
    return (s.price > s.ma50 > s.ma150 > s.ma200
            and s.ma200 > s.ma200_month_ago
            and s.price >= 1.30 * s.low52
            and s.price >= 0.75 * s.high52
            and s.rs_percentile >= config.RS_MIN_PERCENTILE)


@dataclass
class Screen:
    candidates: list[Stats]
    liquid_count: int
    passing_count: int
    breadth: float | None     # share of liquid stocks above their 200-day average
    stats: dict[str, Stats]   # every liquid stock, by symbol (for held stocks outside the list)


def screen(bars: dict[str, Bars], names: dict[str, str]) -> Screen:
    """Liquid stocks ranked by relative strength; the top SCREEN_TOP_N passing the trend template."""
    liquid: list[Stats] = []
    for symbol, rows in bars.items():
        s = stats_for(symbol, rows)
        if s and s.price >= config.MIN_PRICE and s.dollar_volume >= config.MIN_DOLLAR_VOLUME:
            liquid.append(s)
    ranked = sorted(liquid, key=lambda s: s.rs_score)
    n = len(ranked)
    for i, s in enumerate(ranked):
        s.rs_percentile = 100.0 * (i + 1) / n
    for s in liquid:
        s.passes = trend_template(s) and not is_leveraged(names.get(s.symbol, ""))
    passing = [s for s in liquid if s.passes]
    passing.sort(key=lambda s: (-s.rs_percentile, -(s.price / s.high52)))
    breadth = sum(1 for s in liquid if s.price > s.ma200) / n if n else None
    return Screen(passing[: config.SCREEN_TOP_N], n, len(passing), breadth, {s.symbol: s for s in liquid})


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.1f}%"


def describe(s: Stats, name: str = "") -> str:
    """One compact line of numbers per stock for the prompts."""
    return (f"{s.symbol} ({name[:60] or 'unknown name'}): price {s.price:.2f}, RS percentile {s.rs_percentile:.0f}, "
            f"1m {pct(s.r1m)}, 3m {pct(s.r3m)}, 6m {pct(s.r6m)}, 12m {pct(s.r12m)}, "
            f"{pct(s.price / s.high52 - 1)} from 52-week high, {pct(s.price / s.low52 - 1)} above 52-week low, "
            f"vs 50/200-day avg {pct(s.price / s.ma50 - 1)}/{pct(s.price / s.ma200 - 1)}, "
            f"traded ${s.dollar_volume / 1e6:,.0f}M/day, 5-day volume x{s.volume_surge:.1f} of normal, "
            f"10-day range {s.tightness * 100:.1f}%; last {len(s.closes)} closes: "
            + ", ".join(f"{c:.2f}" for c in s.closes))


def regime_text(bars: dict[str, Bars], result: Screen) -> str:
    """Market backdrop: index trends and breadth, the 'M' (market direction) of CAN SLIM."""
    lines = []
    for symbol in ("SPY", "QQQ", "IWM"):
        rows = bars.get(symbol) or []
        closes = [c for _, c, _ in rows]
        if len(closes) >= 200:
            last = closes[-1]
            lines.append(f"- {symbol}: {pct(last / statistics.mean(closes[-50:]) - 1)} vs 50-day average, "
                         f"{pct(last / statistics.mean(closes[-200:]) - 1)} vs 200-day average, "
                         f"1 month {pct(_ret(closes, 21))}")
    if result.breadth is not None:
        lines.append(f"- {result.breadth * 100:.0f}% of {result.liquid_count} liquid US stocks are above their "
                     f"200-day average; {result.passing_count} pass the trend template today.")
    return "\n".join(lines)


def trend_up(bars: dict[str, Bars]) -> list[str]:
    """Rule-portfolio ETFs whose last close is above their 200-day average (for the trend-rule shadow)."""
    out = []
    for s in config.SHADOW_ETFS:
        closes = [c for _, c, _ in bars.get(s, [])]
        if len(closes) >= 200 and closes[-1] > statistics.mean(closes[-200:]):
            out.append(s)
    return out
