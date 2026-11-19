"""Backtest of the day-trading rules on past prices.

Unlike Claude's stock picks, these rules are fixed code with no AI, so testing them on the past is fair:
nothing in them knows what happened next. The same functions as the live run choose the stocks and the
entries (daytrade.select, entry_signal, stop_for).

Prices: the first 5-minute bar of each day from the free IEX feed (what the live run sees at 9:35), then
1-minute bars from all exchanges for the rest of the day. Limits, shown on the dashboard: only shares listed
today are tested (companies delisted since are missing, which flatters results); a breakout is filled at the
range high plus one cent (or the minute's open if higher) before the modelled costs; no market impact.

Run with:  python -m bot.dt_backtest [START END]   (in the cloud: workflow day-trade-backtest)
"""
from __future__ import annotations

import bisect
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import accounting, config, screener
from .alpaca import AlpacaError
from .daytrade import (DailyStats, Session, entry_signal, fee_models, iso_utc, parse_ts, select, sessions_between,
                       settings_view, stop_for)

FIRST_BAR_BATCH = 400       # symbols per request for the first 5-minute bars
MAX_MINUTES = 320           # stop before GitHub's 6-hour limit and save what is done
DEFAULT_START = "2024-01-02"  # the paper's data ended in December 2023: everything after is new to it


class Pacer:
    """Requests share the free plan's 200 a minute with the live runs: slower while the market is open."""

    def __init__(self, broker):
        self.broker, self.count, self.pause = broker, 0, 0.9
        self.check()

    def check(self) -> None:
        try:
            self.pause = 0.9 if self.broker.clock().get("is_open") else 0.3
        except Exception:
            self.pause = 0.9

    def tick(self) -> float:
        self.count += 1
        if self.count % 200 == 0:
            self.check()
        return self.pause


# --- pure parts (tested offline) -------------------------------------------------------------
def daily_tables(rows: list[dict]) -> tuple[list[str], list[float], list[float]]:
    """Dates, running sum of true ranges and running sum of volumes, for O(1) 14-day averages."""
    dates = [str(r["t"])[:10] for r in rows]
    tr_sum, vol_sum = [0.0], [0.0]
    for k, r in enumerate(rows):
        vol_sum.append(vol_sum[-1] + float(r.get("v") or 0))
        if k:
            pc, h, low = float(rows[k - 1]["c"]), float(r["h"]), float(r["l"])
            tr_sum.append(tr_sum[-1] + max(h - low, abs(h - pc), abs(low - pc)))
    return dates, tr_sum, vol_sum


def stats_before(symbol: str, tables, day: str, previous_session: str) -> DailyStats | None:
    """The same figures as daytrade.daily_stats, from the bars strictly before `day`; the share must have
    traded on the previous session."""
    dates, tr_sum, vol_sum = tables
    n = config.DT_LOOKBACK_DAYS
    p = bisect.bisect_left(dates, day)
    if p < n + 1 or dates[p - 1] != previous_session:
        return None
    return DailyStats(symbol, 0.0, (vol_sum[p] - vol_sum[p - n]) / n, (tr_sum[p - 1] - tr_sum[p - 1 - n]) / n)


def day_trades(cands: list, minute_bars: dict[str, list[dict]], session: Session) -> tuple[list[dict], int]:
    """One day under the live rules: entries in time order, at most DT_MAX_POSITIONS, each with its exit.
    Returns the trades and how many breakouts were skipped as too far to chase."""
    start = session.open + timedelta(minutes=5)
    signals = []
    for c in cands:
        rows = [b for b in minute_bars.get(c.symbol, []) if start <= parse_ts(b["t"]) < session.entry_end]
        for b in rows:
            if float(b["h"]) > c.high:
                fill = max(round(c.high + 0.01, 2), float(b["o"]))
                signals.append((parse_ts(b["t"]), c.rank, c, fill))
                break
    signals.sort(key=lambda s: (s[0], s[1]))
    trades, skipped = [], 0
    for when, _rank, c, fill in signals:
        if entry_signal(c.high, c.atr, fill) != "enter":
            skipped += 1
            continue
        if len(trades) >= config.DT_MAX_POSITIONS:
            break
        stop = stop_for(fill, c.atr)
        path = [b for b in minute_bars[c.symbol] if when <= parse_ts(b["t"]) < session.close_at]
        exit_price, reason = None, "sold before the close"
        first = path[0]
        if float(first["l"]) <= stop and float(first["c"]) < fill:
            exit_price, reason = stop, "stop-loss"  # the breakout minute itself fell back through the stop
        else:
            for b in path[1:]:
                if float(b["o"]) <= stop:
                    exit_price, reason = float(b["o"]), "stop-loss"  # opened below: sold at that worse price
                    break
                if float(b["l"]) <= stop:
                    exit_price, reason = stop, "stop-loss"
                    break
        if exit_price is None:
            exit_price = float(path[-1]["c"])
        trades.append({"date": session.day, "symbol": c.symbol, "time": iso_utc(when), "entry": fill, "stop": stop,
                       "exit": exit_price, "reason": reason, "ret": exit_price / fill - 1,
                       "r": (exit_price - fill) / (fill - stop) if fill > stop else None, "rv": c.rv})
    return trades, skipped


class Accounts:
    """Three versions of the same $1,000: before costs, your broker's fees, a no-commission broker."""

    def __init__(self):
        real = config.DT_REAL_CAPITAL_USD
        self.value = {"before": real, "after_a": real, "after_b": real}

    def apply(self, trades: list[dict]) -> None:
        for key in self.value:
            slot = self.value[key] / config.DT_MAX_POSITIONS
            change = 0.0
            for t in trades:
                change += slot * t["ret"]
                if key != "before":
                    fee_buy = fee_models(slot)[0 if key == "after_a" else 1]
                    fee_sell = fee_models(slot * (1 + t["ret"]))[0 if key == "after_a" else 1]
                    change -= fee_buy + fee_sell
            self.value[key] += change


def summarize(points: list[dict], trades: list[dict], rf: float) -> dict:
    real = config.DT_REAL_CAPITAL_USD
    lines = ["before", "after_a", "after_b", "spy_a"]
    spy = [p["spy_a"] for p in points]
    results = {k: accounting.risk_metrics([p[k] for p in points], real, rf, None if k == "spy_a" else spy)
               for k in lines}
    years, prev = [], {k: real for k in lines}
    for year in sorted({p["date"][:4] for p in points}):
        last = [p for p in points if p["date"][:4] == year][-1]
        years.append({"year": year, "trades": sum(1 for t in trades if t["date"][:4] == year),
                      **{k: last[k] / prev[k] - 1 for k in lines}})
        prev = {k: last[k] for k in lines}
    wins = [t for t in trades if t["ret"] > 0]
    gain, loss = sum(t["ret"] for t in wins), -sum(t["ret"] for t in trades if t["ret"] <= 0)
    rs = [t["r"] for t in trades if t["r"] is not None]
    return {
        "results": results,
        "years": years,
        "trades": len(trades),
        "win_rate": len(wins) / len(trades) if trades else None,
        "avg_trade_pct": sum(t["ret"] for t in trades) / len(trades) if trades else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "profit_factor": gain / loss if loss > 0 else None,
        "stopped_share": sum(1 for t in trades if t["reason"] == "stop-loss") / len(trades) if trades else None,
        "days_with_trades": len({t["date"] for t in trades}),
    }


# --- the run ----------------------------------------------------------------------------------
def run(broker, start: str, end: str, log=print, clock=time.monotonic, risk_free=lambda day: None) -> dict:
    began = clock()
    pacer = Pacer(broker)
    real = config.DT_REAL_CAPITAL_USD
    n = config.DT_LOOKBACK_DAYS
    first_day = (date.fromisoformat(start) - timedelta(days=45)).isoformat()
    sessions = sessions_between(broker, first_day, end)
    test_idx = [i for i, s in enumerate(sessions) if start <= s.day <= end and i >= n + 1]
    if not test_idx:
        raise SystemExit("No trading session in that period.")
    names = screener.tradable_assets(broker.assets())
    symbols = sorted(s for s, nm in names.items() if not screener.is_leveraged(nm))
    log(f"{len(symbols)} symbols, {len(test_idx)} sessions from {sessions[test_idx[0]].day} to {sessions[test_idx[-1]].day}")

    # 1. Daily bars: who qualified on each test day, and with which ATR.
    feed = config.BARS_FEED
    end_next = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    eligible: dict[int, dict[str, tuple]] = {i: {} for i in test_idx}  # (average volume, ATR): small in memory
    spy_closes: dict[str, float] = {}
    for b in range(0, len(symbols), config.DT_BATCH):
        batch = symbols[b:b + config.DT_BATCH]
        try:
            got = broker.bars(batch, "1Day", sessions[0].day + "T00:00:00Z", end_next + "T00:00:00Z", feed,
                              adjustment="split", pause=pacer.tick())
        except AlpacaError as e:
            if feed == "iex" or not any(k in str(e) for k in ("403", "subscription", "permit")):
                raise
            feed = "iex"
            got = broker.bars(batch, "1Day", sessions[0].day + "T00:00:00Z", end_next + "T00:00:00Z", feed,
                              adjustment="split", pause=pacer.tick())
        for sym, rows in got.items():
            tables = daily_tables(rows)
            if sym == config.BENCHMARK:
                spy_closes = {str(r["t"])[:10]: float(r["c"]) for r in rows}
            for i in test_idx:
                st = stats_before(sym, tables, sessions[i].day, sessions[i - 1].day)
                if st and st.avg_volume >= config.DT_MIN_AVG_VOLUME and st.atr >= config.DT_MIN_ATR:
                    eligible[i][sym] = (st.avg_volume, st.atr)
        if b // config.DT_BATCH % 10 == 0:
            log(f"daily bars {b + len(batch)}/{len(symbols)}")
    if config.BENCHMARK not in symbols:
        got = broker.bars([config.BENCHMARK], "1Day", sessions[0].day + "T00:00:00Z", end_next + "T00:00:00Z",
                          feed, adjustment="split", pause=pacer.tick())
        spy_closes = {str(r["t"])[:10]: float(r["c"]) for r in got.get(config.BENCHMARK, [])}
    log(f"eligible per day: {sum(len(v) for v in eligible.values()) / len(test_idx):.0f} on average")

    # 2. Day by day: first bars (IEX), selection, then the minute path of the candidates.
    test_set = set(test_idx)
    volumes: dict[int, dict[str, float]] = {}
    accounts = Accounts()
    base_spy = spy_closes.get(sessions[test_idx[0] - 1].day)
    fee_spy_a, fee_spy_b = fee_models(real)
    points, trades, skipped, in_play_days, stopped_at = [], [], 0, 0, None
    for i in range(test_idx[0] - n, test_idx[-1] + 1):
        if (clock() - began) / 60 > MAX_MINUTES:
            stopped_at = sessions[i].day
            log(f"time limit: stopped before {stopped_at}")
            break
        s = sessions[i]
        need = set()
        for j in range(i, i + n + 1):
            if j in test_set:
                need |= set(eligible[j])
        want = sorted(need)
        bars: dict[str, dict] = {}
        start_iso, end_iso = iso_utc(s.open), iso_utc(s.open + timedelta(minutes=4, seconds=59))
        for b in range(0, len(want), FIRST_BAR_BATCH):
            got = broker.bars(want[b:b + FIRST_BAR_BATCH], "5Min", start_iso, end_iso, config.DT_FEED, pause=pacer.tick())
            for sym, rows in got.items():
                if rows and parse_ts(rows[0]["t"]) == s.open:
                    bars[sym] = rows[0]
        volumes[i] = {sym: float(bar.get("v") or 0) for sym, bar in bars.items()}
        volumes.pop(i - n - 1, None)
        if i not in test_set:
            continue
        today_ok = {sym: DailyStats(sym, 0.0, v, a) for sym, (v, a) in eligible[i].items()}
        avg = {sym: sum(volumes.get(j, {}).get(sym, 0.0) for j in range(i - n, i)) / n for sym in today_ok}
        cands, in_play = select({k: v for k, v in bars.items() if k in today_ok}, avg, today_ok)
        in_play_days += in_play > 0
        day = []
        if cands:
            minute: dict[str, list[dict]] = {}
            for attempt_feed in ("sip", "iex"):
                try:
                    minute = broker.bars([c.symbol for c in cands], "1Min", iso_utc(s.open + timedelta(minutes=5)),
                                         iso_utc(s.close_at), attempt_feed, pause=pacer.tick())
                    break
                except AlpacaError:
                    if attempt_feed == "iex":
                        raise
            cands = [c for c in cands if minute.get(c.symbol)]
            day, skip = day_trades(cands, minute, s)
            skipped += skip
        accounts.apply(day)
        trades += day
        spy = spy_closes.get(s.day)
        held = real * spy / base_spy if (spy and base_spy) else None
        points.append({"date": s.day, **{k: round(v, 2) for k, v in accounts.value.items()},
                       "spy_a": round(held - fee_spy_a, 2) if held else None,
                       "spy_b": round(held - fee_spy_b, 2) if held else None})
        if len(points) % 25 == 0:
            log(f"{s.day}: {len(trades)} trades, before costs {accounts.value['before']:.2f}, "
                f"your fees {accounts.value['after_a']:.2f}, no-commission {accounts.value['after_b']:.2f}")
    points = [p for p in points if p["spy_a"] is not None]
    try:
        rate = risk_free(end)
    except Exception:
        rate = None
    rf = rate / 100 if rate is not None else 0.0
    out = {
        "generated_at": iso_utc(datetime.now(timezone.utc)),
        "start": points[0]["date"] if points else start,
        "end": points[-1]["date"] if points else end,
        "stopped_early_at": stopped_at,
        "sessions": len(points),
        "symbols": len(symbols),
        "avg_eligible": round(sum(len(v) for v in eligible.values()) / len(test_idx)),
        "days_in_play": in_play_days,
        "skipped_chases": skipped,
        "risk_free_rate": rf,
        "rules": settings_view(),
        "daily_feed": feed,
        "curve": points,
        "recent_trades": trades[-200:][::-1],
        **summarize(points, trades, rf),
        "caveats": [
            "Only shares listed today are tested: companies delisted since are missing, which flatters results.",
            "A breakout is filled at the range high plus one cent (or the minute's open if higher); real fills on "
            "fast stocks are often worse. The no-commission model charges 0.05% per order for this.",
            "Relative volume uses the free IEX feed, a small share of all trading, as the live run does.",
            "The rules come from a 2024 paper tested on 2016-2023; the years after are new to them.",
        ],
    }
    return out


def main() -> None:
    from .alpaca import AlpacaPaper

    args = [a for a in sys.argv[1:] if a.strip() and not a.startswith("-")]
    start = args[0] if args else DEFAULT_START
    end = args[1] if len(args) > 1 else (date.today() - timedelta(days=1)).isoformat()
    broker = AlpacaPaper(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""))
    from . import research
    result = run(broker, start, end, log=lambda m: print(m, flush=True),
                 risk_free=lambda day: research.latest_value(config.RISK_FREE_SERIES, day))
    path = Path(config.DT_BACKTEST_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1))
    r = result["results"]
    print(f"{result['start']} to {result['end']}: {result['trades']} trades, win rate "
          f"{(result['win_rate'] or 0) * 100:.0f}%. On ${config.DT_REAL_CAPITAL_USD:,.0f}: before costs "
          f"{r['before']['total_return'] * 100:+.1f}%, your fees {r['after_a']['total_return'] * 100:+.1f}%, "
          f"no-commission broker {r['after_b']['total_return'] * 100:+.1f}%, SPY {r['spy_a']['total_return'] * 100:+.1f}%.")


if __name__ == "__main__":
    main()
