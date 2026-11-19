"""Day trading on its own paper account: opening-range breakout on "stocks in play", long only, with Claude
choosing which of the morning's candidates the rules may buy.

Method (Zarattini, Barbon & Aziz, 2024, "A Profitable Day Trading Strategy For The U.S. Equity Market"):
- universe: US shares whose first 5-minute bar opens at $5 or more, that traded at least 1M shares a day and
  moved at least $0.50 a day on average over the last 14 days (ATR, the average true range);
- at 9:35 New York time, the "stocks in play" are the 20 whose first 5 minutes traded the most volume
  compared with their own 14-day average for those 5 minutes (at least 100% of it);
- of those, the ones that rose in their first 5 minutes are bought when the price breaks above the high of
  those 5 minutes; a stop-loss sits 10% of the ATR below the entry, kept by Alpaca itself;
- every position still open is sold 10 minutes before the close.
Changes from the paper: long only (the paper also sells short), no borrowing (the paper uses up to 4 times
the account), at most 5 positions of a fifth of the account each, no new entry after 12:00, and a breakout
already 10% of an ATR past the high when first seen is skipped rather than chased.
Claude's part (bot/dt_ai.py): right after the scan it reads each candidate's numbers and headlines, turns some
down and orders the rest. Without an answer the rules alone choose that day. At the close, the same rules
without Claude are replayed on the day's minute prices, so the dashboard shows what Claude added or cost.

Run with:  python -m bot.daytrade   (started every 15 minutes during US market hours, see README)
Each run looks at the time and does what is due; running it twice never trades twice.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from . import accounting, config, screener
from .alpaca import AlpacaError

NEW_YORK = ZoneInfo("America/New_York")


# --- time ------------------------------------------------------------------------------------
def parse_ts(value) -> datetime:
    """Alpaca time ('Z' or an offset, up to nanoseconds) -> aware datetime. No offset means UTC."""
    s = str(value).strip().replace("Z", "+00:00")
    m = re.match(r"^(\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d)?)(\.\d+)?(.*)$", s)
    if not m:
        raise ValueError(f"not a time: {value!r}")
    frac = (m.group(2) or "")[:7]
    return datetime.fromisoformat(m.group(1).replace(" ", "T") + frac + (m.group(3) or "+00:00"))


def at_ny(day: str, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":")[:2])
    y, mo, d = (int(x) for x in day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=NEW_YORK)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ny_date(dt: datetime) -> str:
    return dt.astimezone(NEW_YORK).date().isoformat()


@dataclass
class Session:
    day: str
    open: datetime
    close: datetime

    @property
    def scan_at(self) -> datetime:  # the first 5-minute bar is complete; a few seconds for the feed
        return self.open + timedelta(minutes=5, seconds=20)

    @property
    def entry_end(self) -> datetime:
        return min(at_ny(self.day, config.DT_ENTRY_END), self.close - timedelta(minutes=30))

    @property
    def close_at(self) -> datetime:
        return self.close - timedelta(minutes=config.DT_CLOSE_BEFORE_MIN)


def sessions_between(broker, start: str, end: str) -> list[Session]:
    out = []
    for row in broker.calendar(start, end) or []:
        day = str(row.get("date", ""))[:10]
        if day and row.get("open") and row.get("close"):
            out.append(Session(day, at_ny(day, row["open"]), at_ny(day, row["close"])))
    return sorted(out, key=lambda s: s.day)


def session_for(broker, day: str) -> Session | None:
    return next((s for s in sessions_between(broker, day, day) if s.day == day), None)


# --- the rules (pure functions, shared with the backtest) ----------------------------------------
@dataclass
class DailyStats:
    symbol: str
    close: float
    avg_volume: float
    atr: float


def daily_stats(symbol: str, bars: list[dict]) -> DailyStats | None:
    """From completed daily bars, oldest first: 14-day average volume and average true range."""
    n = config.DT_LOOKBACK_DAYS
    if len(bars) < n + 1:
        return None
    recent = bars[-(n + 1):]
    ranges = []
    for prev, b in zip(recent, recent[1:]):
        pc, h, low = float(prev["c"]), float(b["h"]), float(b["l"])
        ranges.append(max(h - low, abs(h - pc), abs(low - pc)))
    volume = sum(float(b.get("v") or 0) for b in recent[1:]) / n
    return DailyStats(symbol, float(recent[-1]["c"]), volume, sum(ranges) / n)


def eligible(st: DailyStats | None) -> bool:
    return bool(st) and st.avg_volume >= config.DT_MIN_AVG_VOLUME and st.atr >= config.DT_MIN_ATR


@dataclass
class Candidate:
    symbol: str
    rank: int        # 1 = highest relative volume of the day
    rv: float        # first 5 minutes' volume / its 14-day average
    open: float
    high: float      # the breakout level
    low: float
    close: float
    volume: float
    atr: float
    state: str = "waiting"
    detail: str = ""
    priority: int = 0              # entry order once Claude has rated the candidates (0 = by rank)
    ai_trade: bool | None = None   # Claude's answer, None without one
    ai_score: int | None = None
    ai_reason: str = ""


def select(first_bars: dict[str, dict], avg_first: dict[str, float],
           stats: dict[str, DailyStats]) -> tuple[list[Candidate], int]:
    """The day's long candidates, highest relative volume first, and how many stocks were in play."""
    rows = []
    for sym, b in first_bars.items():
        st, avg = stats.get(sym), avg_first.get(sym) or 0.0
        o, v = float(b["o"]), float(b.get("v") or 0)
        if not eligible(st) or avg <= 0 or o < config.DT_MIN_PRICE:
            continue
        rv = v / avg
        if rv >= config.DT_MIN_RELATIVE_VOLUME:
            rows.append((rv, sym, b, st))
    rows.sort(key=lambda r: (-r[0], r[1]))
    in_play = rows[:config.DT_TOP_N]
    out = [Candidate(sym, i, round(rv, 3), float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]),
                     float(b.get("v") or 0), round(st.atr, 4))
           for i, (rv, sym, b, st) in enumerate(in_play, 1) if float(b["c"]) > float(b["o"])]
    return out, len(in_play)


def entry_signal(high: float, atr: float, price: float) -> str:
    """'wait' below the range high, 'enter' just above it, 'skip' if it already ran too far to chase."""
    if price <= high:
        return "wait"
    if price > high + config.DT_MAX_CHASE_ATR * atr:
        return "skip"
    return "enter"


def stop_for(price: float, atr: float) -> float:
    return round(price - config.DT_STOP_ATR * atr, 2)


def shares_for(slot_usd: float, cash: float, price: float) -> int:
    """Whole shares for one entry: its share of the account, never more than the cash (no borrowing)."""
    if price <= 0:
        return 0
    return int(min(slot_usd, max(cash, 0.0) * 0.98) // price)


def fee_models(notional_real: float) -> tuple[float, float]:
    """Fee of one filled order at real size: (your broker, a no-commission broker)."""
    if notional_real <= 0:
        return 0.0, 0.0
    return (config.FEE_PER_ORDER_USD + config.FEE_RATE * notional_real,
            config.DT_LOWCOST_FEE_PER_ORDER_USD + config.DT_LOWCOST_FEE_RATE * notional_real)


# --- market data -------------------------------------------------------------------------------
def _daily_bars(broker, symbols: list[str], start: str, end_exclusive: str, pause: float) -> dict[str, list[dict]]:
    """Completed daily bars (all exchanges, or IEX if the plan refuses), adjusted for splits."""
    feed, out = config.BARS_FEED, {}
    for i in range(0, len(symbols), config.DT_BATCH):
        batch = symbols[i:i + config.DT_BATCH]
        try:
            got = broker.bars(batch, "1Day", start + "T00:00:00Z", end_exclusive + "T00:00:00Z", feed,
                              adjustment="split", pause=pause)
        except AlpacaError as e:
            if feed == "iex" or not any(k in str(e) for k in ("403", "subscription", "permit")):
                raise
            feed = "iex"
            got = broker.bars(batch, "1Day", start + "T00:00:00Z", end_exclusive + "T00:00:00Z", feed,
                              adjustment="split", pause=pause)
        for sym, rows in got.items():
            out[sym] = [b for b in rows if str(b["t"])[:10] < end_exclusive]
    return out


def first_bars(broker, symbols: list[str], session: Session, pause: float) -> dict[str, dict]:
    """The first 5-minute bar of the session (free IEX feed) for each symbol that traded in it."""
    start, end = iso_utc(session.open), iso_utc(session.open + timedelta(minutes=4, seconds=59))
    out = {}
    for i in range(0, len(symbols), config.DT_BATCH):
        for sym, rows in broker.bars(symbols[i:i + config.DT_BATCH], "5Min", start, end, config.DT_FEED,
                                     pause=pause).items():
            if rows and parse_ts(rows[0]["t"]) == session.open:
                out[sym] = rows[0]
    return out


@dataclass
class Prep:
    universe: int = 0
    stats: dict = field(default_factory=dict)        # symbol -> DailyStats, eligible ones only
    avg_first: dict = field(default_factory=dict)    # symbol -> average first-5-minute volume
    names: dict = field(default_factory=dict)        # symbol -> company name, eligible ones only


def prepare(broker, session: Session, pause: float) -> Prep:
    """Before the open: who qualifies today, and each one's usual volume in the first 5 minutes."""
    names = screener.tradable_assets(broker.assets())
    symbols = sorted(s for s, n in names.items() if not screener.is_leveraged(n))
    past = [s for s in sessions_between(broker, (date.fromisoformat(session.day) - timedelta(days=45)).isoformat(),
                                        (date.fromisoformat(session.day) - timedelta(days=1)).isoformat())
            if s.day < session.day]
    n = config.DT_LOOKBACK_DAYS
    if len(past) < n + 1:
        return Prep(universe=len(symbols))
    bars = _daily_bars(broker, symbols, past[-(n + 1)].day, session.day, pause)
    stats = {}
    for sym, rows in bars.items():
        st = daily_stats(sym, rows[-(n + 1):]) if len(rows) >= n + 1 and rows[-1]["t"][:10] == past[-1].day else None
        if eligible(st):
            stats[sym] = st
    totals = dict.fromkeys(stats, 0.0)
    for s in past[-n:]:
        for sym, b in first_bars(broker, sorted(stats), s, pause).items():
            totals[sym] += float(b.get("v") or 0)
    return Prep(len(symbols), stats, {s: v / n for s, v in totals.items()}, {s: names.get(s, "") for s in stats})


# --- the ledger ----------------------------------------------------------------------------------
def load_state(path: str = None) -> dict | None:
    p = Path(path or config.DT_STATE_PATH)
    return json.loads(p.read_text()) if p.exists() else None


def save_state(state: dict, path: str = None) -> None:
    p = Path(path or config.DT_STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(p)


def day_record(state: dict, day: str) -> dict:
    rec = next((d for d in state["days"] if d["date"] == day), None)
    if rec is None:
        rec = {"date": day, "status": "started", "notes": [], "candidates": [], "entries": [], "leftovers": [],
               "close_orders": []}
        state["days"].append(rec)
        state["days"].sort(key=lambda d: d["date"])
    return rec


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --- the run -----------------------------------------------------------------------------------
@dataclass
class Env:
    now: Callable[[], datetime]
    sleep: Callable[[float], None]
    publish: Callable[[dict], None] = lambda state: None   # pushes the dashboard during long waits
    pause: float = 0.35                                    # between price requests
    started: datetime | None = None
    review: Callable | None = None   # Claude's rating of the candidates (dt_ai.review_candidates); None = rules only


def _sleep_until(env: Env, when: datetime) -> None:
    while True:
        left = (when - env.now()).total_seconds()
        if left <= 0:
            return
        env.sleep(min(left, 60.0))


def _cancel_orders(broker, env: Env, symbols: set[str] | None = None, timeout_s: float = 30.0) -> None:
    """Cancel open orders (only those of `symbols` if given) and wait until they are gone."""
    def mine():
        return [o for o in broker.open_orders() if symbols is None or o.get("symbol") in symbols]
    for o in mine():
        try:
            broker.cancel_order(o["id"])
        except AlpacaError:
            pass  # already filled or cancelled
    deadline = env.now() + timedelta(seconds=timeout_s)
    while mine() and env.now() < deadline:
        env.sleep(1.0)


def _sell_all(broker, env: Env, symbols: list[str]) -> list[dict]:
    """Market-sell whole positions (their stop-loss orders cancelled first). One failure never stops the others."""
    out = []
    if not symbols:
        return out
    _cancel_orders(broker, env, set(symbols))
    for sym in symbols:
        rec = {"symbol": sym, "status": "", "order_id": None, "fill_price": None, "qty": 0.0}
        try:
            placed = broker.close_position(sym)
            rec["order_id"] = placed.get("id")
            final = broker.wait_final(placed["id"], timeout_s=60)
            rec.update(status=final.get("status", ""), fill_price=_f(final.get("filled_avg_price")) or None,
                       qty=_f(final.get("filled_qty")))
        except Exception as e:
            rec["status"] = f"error: {e}"[:200]
        out.append(rec)
    return out


def _held(broker) -> dict[str, dict]:
    return {p["symbol"]: p for p in broker.positions_detail() if abs(_f(p.get("qty"))) > 1e-9}


def _bought_on(broker, day: str) -> set[str]:
    """Symbols with a purchase filled on this New York date."""
    out = set()
    for o in broker.filled_orders(iso_utc(at_ny(day, "00:00"))):
        if o.get("side") == "buy" and _f(o.get("filled_qty")) > 0 and o.get("filled_at") \
                and ny_date(parse_ts(o["filled_at"])) == day:
            out.add(o.get("symbol"))
    return out


def _mark_missed(broker, rec: dict, session: Session, now: datetime, pause: float) -> None:
    """A scan made late: a stock that already went above its range high since 9:35 is marked missed
    (the rule would have bought it then), so a later second breakout is never mistaken for the first."""
    waiting = [c for c in rec["candidates"] if c["state"] == "waiting"]
    if not waiting:
        return
    bars = broker.bars([c["symbol"] for c in waiting], "1Min", iso_utc(session.open + timedelta(minutes=5)),
                       iso_utc(now), config.DT_FEED, pause=pause)
    for c in waiting:
        top = max((float(b["h"]) for b in bars.get(c["symbol"], [])), default=0.0)
        if top > c["high"]:
            c["state"], c["detail"] = "missed", f"went above {c['high']:.2f} before this run started"


def _new_state(broker, today: str) -> dict:
    spy = broker.latest_prices([config.BENCHMARK]).get(config.BENCHMARK)
    return {"version": 1, "start_date": today, "start_equity": _f(broker.account().get("equity")),
            "bench_start_price": spy, "days": []}


FAILED = {"rejected", "canceled", "expired"}


def _counts(entry: dict) -> bool:
    """An entry that uses one of the day's places: an order was accepted by Alpaca."""
    return bool(entry.get("order_id")) and entry.get("status") not in FAILED


def _ai_step(broker, state: dict, rec: dict, prep: Prep, first: dict, env: Env, today: str) -> None:
    """Claude rates the candidates still waiting: those it turns down are not bought, the others are bought in
    the order of its scores. Without a usable answer nothing changes: the rules alone choose today."""
    waiting = [c for c in rec["candidates"] if c["state"] == "waiting"]
    if not waiting or env.review is None or env.now() >= at_ny(today, config.DT_ENTRY_END):
        return
    fields = set(Candidate.__dataclass_fields__)
    objs = [Candidate(**{k: v for k, v in c.items() if k in fields}) for c in waiting]
    try:
        r = env.review(broker, objs, prep, today, env.now(), first, state.get("api_cost_usd", 0.0))
    except Exception as e:  # never let the call stop the trading day
        from .dt_ai import Review
        r = Review("api_error", detail=type(e).__name__)
    state["api_cost_usd"] = round(state.get("api_cost_usd", 0.0) + r.cost_usd, 6)
    rec["ai"] = {"status": r.status, "model": r.model, "cost_usd": round(r.cost_usd, 6), "summary": r.summary,
                 "detail": r.detail, "at": iso_utc(env.now()), "rated": len(r.picks),
                 "approved": sum(1 for p in r.picks.values() if p["trade"])}
    if r.status != "ok":
        rec["notes"].append(f"Claude gave no usable answer ({r.status}{': ' + r.detail if r.detail else ''}): "
                            "the rules alone choose today")
        return
    approved = sorted((p["score"], c["rank"], c["symbol"]) for c in waiting
                      for p in [r.picks.get(c["symbol"])] if p and p["trade"])
    order = {sym: i for i, (_s, _r, sym) in enumerate(sorted(approved, key=lambda x: (-x[0], x[1])), 1)}
    for c in waiting:
        p = r.picks.get(c["symbol"])
        if p:
            c["ai_trade"], c["ai_score"], c["ai_reason"] = p["trade"], p["score"], p["reason"]
        if p and p["trade"]:
            c["priority"] = order[c["symbol"]]
        else:
            c["state"] = "ai pass"
            c["detail"] = "Claude: " + (p["reason"] if p else "not rated, so not bought")
    if not any(c["state"] == "waiting" for c in rec["candidates"]) and rec["status"] == "watching":
        rec["status"] = "ai passed"


def _enter(broker, c: dict, price: float, rec: dict, now: datetime, save: Callable[[], None]) -> None:
    """One breakout: buy at market with a stop-loss that Alpaca keeps (an OTO order)."""
    qty = shares_for(rec["slot_usd"], broker.cash(), price)
    stop = stop_for(price, c["atr"])
    if qty < 1 or stop >= price - 0.01:
        c["state"], c["detail"] = "skipped", "position would be under one share"
        return
    entry = {"symbol": c["symbol"], "time": iso_utc(now), "seen_price": price, "qty": qty, "stop": stop,
             "order_id": None, "status": "", "fill_price": None}
    rec["entries"].append(entry)
    save()  # recorded before the order is sent: a crash can never make the next run sell it as a leftover
    coid = f"dt-{rec['date']}-{c['symbol']}"  # Alpaca refuses a second order with the same id: no double entry
    try:
        try:
            placed = broker.oto_market_buy(c["symbol"], qty, stop, coid)
        except AlpacaError as e:
            if "422" not in str(e) or "time_in_force" not in str(e).lower():
                raise
            placed = broker.oto_market_buy(c["symbol"], qty, stop, coid + "-d", "day")  # refused, so never placed
        entry["order_id"] = placed.get("id")
        final = broker.wait_final(placed["id"], timeout_s=20)
        entry.update(status=final.get("status", ""), fill_price=_f(final.get("filled_avg_price")) or None)
        if entry["status"] in FAILED:
            c["state"], c["detail"] = "error", f"order {entry['status']} by Alpaca"
            return
        c["state"] = "entered"
        c["detail"] = f"bought {qty} at {entry['fill_price'] or price:.2f}, stop {stop:.2f}"
    except Exception as e:
        entry["status"] = f"error: {e}"[:200]
        c["state"], c["detail"] = "error", entry["status"]


def _rules_only(broker, rec: dict, session: Session, pause: float) -> dict:
    """The same rules without Claude, replayed on today's minute prices: what Claude added or cost today.
    Every candidate counts, as if Claude had approved all of them in the rules' order."""
    from .dt_backtest import day_trades  # imported here: dt_backtest imports this module
    fields = set(Candidate.__dataclass_fields__)
    cands = [Candidate(**{k: v for k, v in c.items() if k in fields}) for c in rec.get("candidates", [])]
    if not cands:
        return {"trades": [], "ret": 0.0}
    try:
        bars = broker.bars([c.symbol for c in cands], "1Min", iso_utc(session.open + timedelta(minutes=5)),
                           iso_utc(session.close_at), config.DT_FEED, pause=pause)
        trades, _skipped = day_trades(cands, bars, session)
    except Exception as e:
        return {"error": type(e).__name__}
    return {"trades": [{k: t[k] for k in ("symbol", "entry", "exit", "reason", "ret")} for t in trades],
            "ret": sum(t["ret"] for t in trades) / config.DT_MAX_POSITIONS}


def run_tick(broker, state: dict | None, env: Env, save: Callable[[dict], None]) -> tuple[dict | None, str]:
    """Does whatever is due now: sell leftovers, prepare, scan at 9:35, watch for breakouts, sell before
    the close. Returns the new state (None until the first scan) and a one-line message."""
    env.started = env.started or env.now()
    deadline = env.started + timedelta(minutes=config.DT_MAX_RUN_MIN)
    clock = broker.clock()
    today = ny_date(parse_ts(clock["timestamp"]))
    session = session_for(broker, today)
    now = env.now()
    if session is None:
        return state, f"No trading session on {today}."
    if now < session.open - timedelta(minutes=config.DT_PREP_BEFORE_OPEN_MIN):
        return state, "Too early: a later run prepares the day."
    if now >= session.close:
        if state and today >= state["start_date"]:
            held = _held(broker)
            rec = next((d for d in state["days"] if d["date"] == today), None)
            if held and not (rec or {}).get("closed_at"):
                rec = rec or day_record(state, today)
                note = "positions kept overnight: no run started before the close (" + ", ".join(sorted(held)) + ")"
                if note not in rec["notes"]:
                    rec["notes"].append(note)
                rec["status"] = "close missed"
                save(state)
        return state, "Market closed."
    messages = []
    rec = day_record(state, today) if state else None

    # 1. Positions left from an earlier day (a missed closing run): sold as soon as the market opens.
    def sell_leftovers() -> None:
        if not state or env.now() < session.open:
            return
        held = _held(broker)
        if not held:
            return
        today_symbols = {e["symbol"] for e in rec["entries"]} | _bought_on(broker, today)
        left = sorted(s for s in held if s not in today_symbols)
        if left:
            rec["leftovers"] += _sell_all(broker, env, left)
            rec["notes"].append("sold at the open, left from an earlier day: " + ", ".join(left))
            save(state)
            messages.append(f"sold {len(left)} leftover position(s)")

    sell_leftovers()

    # 2. Prepare, then scan the first 5 minutes.
    if not (rec and rec.get("scanned_at")):
        if now >= session.entry_end:
            if rec is not None and rec["status"] == "started":
                rec["status"] = "no scan"
                rec["notes"].append(f"no scan: the first run of the day started after {config.DT_ENTRY_END}")
                save(state)
        else:
            prep = prepare(broker, session, env.pause)
            if session.scan_at > env.now():
                if session.scan_at > deadline:
                    return state, "Prepared, but this run would end before 9:35: a later run scans."
                _sleep_until(env, session.scan_at)
                sell_leftovers()
            if env.now() < session.entry_end:
                if state is None and _held(broker):
                    return None, ("Refused to start: this paper account already holds positions. Day trading needs an "
                                  "account of its own, since it sells everything it finds.")
                if state is None:
                    state = _new_state(broker, today)
                    rec = day_record(state, today)
                elif rec is None:
                    rec = day_record(state, today)
                bars = first_bars(broker, sorted(prep.stats), session, env.pause)
                cands, in_play = select(bars, prep.avg_first, prep.stats)
                equity = _f(broker.account().get("equity"))
                rec.update(scanned_at=iso_utc(env.now()), universe=prep.universe, eligible=len(prep.stats),
                           traded_first_bar=len(bars), in_play=in_play, equity_open=equity,
                           slot_usd=equity / config.DT_MAX_POSITIONS,
                           candidates=[asdict(c) for c in cands],
                           status="watching" if cands else "no candidate")
                if not prep.stats:
                    rec["notes"].append("no price history: nothing could be screened")
                if env.now() > session.scan_at + timedelta(minutes=2):
                    rec["notes"].append("scanned late, at " + env.now().astimezone(NEW_YORK).strftime("%H:%M")
                                        + " New York time")
                    _mark_missed(broker, rec, session, env.now(), env.pause)
                    if not any(c["state"] == "waiting" for c in rec["candidates"]) and rec["candidates"]:
                        rec["status"] = "no entry"
                save(state)  # the scan is kept even if the step below fails
                _ai_step(broker, state, rec, prep, bars, env, today)
                save(state)
                env.publish(state)
                messages.append(f"scanned: {in_play} in play, {len(cands)} long candidate(s)"
                                + (f"; Claude approved {rec['ai']['approved']}" if rec.get("ai", {}).get("status") == "ok" else ""))

    # 3. Watch for breakouts until 12:00, entering through Alpaca orders that carry their stop-loss.
    if rec and rec.get("scanned_at") and env.now() < session.entry_end:
        last_publish = env.now()
        entered = sum(1 for e in rec["entries"] if _counts(e))
        while True:
            now = env.now()
            waiting = [c for c in rec["candidates"] if c["state"] == "waiting"]
            if not waiting or now >= session.entry_end or now >= deadline - timedelta(minutes=2):
                break
            if entered >= config.DT_MAX_POSITIONS:
                for c in waiting:
                    c["state"], c["detail"] = "not taken", f"all {config.DT_MAX_POSITIONS} places already used"
                break
            held = _held(broker)
            prices = broker.latest_prices([c["symbol"] for c in waiting])
            changed = False
            for c in sorted(waiting, key=lambda c: (c.get("priority") or c["rank"], c["rank"])):
                price = prices.get(c["symbol"])
                if not price or entered >= config.DT_MAX_POSITIONS:
                    continue
                signal = entry_signal(c["high"], c["atr"], price)
                if signal == "skip":
                    c["state"] = "skipped"
                    c["detail"] = f"first seen at {price:.2f}, already more than 10% of an ATR above {c['high']:.2f}"
                    changed = True
                elif signal == "enter" and c["symbol"] not in held:
                    _enter(broker, c, price, rec, now, lambda: save(state))
                    entered += c["state"] == "entered"
                    changed = True
            if changed:
                save(state)
            if (env.now() - last_publish).total_seconds() >= config.DT_PUBLISH_EVERY_MIN * 60:
                env.publish(state)
                last_publish = env.now()
            env.sleep(config.DT_POLL_SECONDS)
        if env.now() >= session.entry_end:
            for c in rec["candidates"]:
                if c["state"] == "waiting":
                    c["state"], c["detail"] = "no breakout", f"stayed under {c['high']:.2f} until {config.DT_ENTRY_END}"
        if rec["status"] == "watching" and not any(c["state"] == "waiting" for c in rec["candidates"]):
            rec["status"] = "holding" if any(_counts(e) for e in rec["entries"]) else "no entry"
        save(state)
        messages.append(f"{sum(1 for e in rec['entries'] if _counts(e))} entry order(s) so far")

    # 4. Sell everything shortly before the close.
    if state and rec is not None and not rec.get("closed_at") \
            and env.now() >= session.close_at - timedelta(minutes=55):
        if _held(broker) or broker.open_orders():
            if session.close_at > deadline:
                return state, "; ".join(messages + ["the closing run comes later"])
            env.publish(state)
            _sleep_until(env, session.close_at)
        _cancel_orders(broker, env)  # this paper account holds nothing else: every open order is ours
        rec["close_orders"] = _sell_all(broker, env, sorted(_held(broker)))
        rec["closed_at"] = iso_utc(env.now())
        rec["equity_close"] = _f(broker.account().get("equity"))
        rec["bench_close"] = broker.latest_prices([config.BENCHMARK]).get(config.BENCHMARK)
        rec["rules_only"] = _rules_only(broker, rec, session, env.pause)
        if rec["status"] in ("watching", "holding", "started"):
            rec["status"] = "closed"
        save(state)
        messages.append(f"sold {len(rec['close_orders'])} position(s) before the close")
    return state, "; ".join(messages) or "Nothing due now."


# --- what the dashboard shows ------------------------------------------------------------------
def trades_from_fills(orders: list[dict], state: dict) -> tuple[list[dict], dict, dict]:
    """Every round trip since the start, first in first out, with both fee models at real size.
    Returns the trades (oldest first), fees per New York date for each model, and the open lots."""
    ratio = config.DT_REAL_CAPITAL_USD / state["start_equity"]
    stops = {e.get("order_id"): e.get("stop") for d in state["days"] for e in d.get("entries", []) if e.get("order_id")}
    why = {(d["date"], c["symbol"]): c.get("ai_reason") or "" for d in state["days"] for c in d.get("candidates", [])}
    fills = []
    for o in orders:
        qty, price = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
        when = o.get("filled_at") or ""
        if qty <= 0 or price <= 0 or not when:
            continue
        day = ny_date(parse_ts(when))
        if day >= state["start_date"]:
            fills.append((parse_ts(when), day, o, qty, price))
    fills.sort(key=lambda f: f[0])
    lots: dict[str, list[dict]] = {}
    trades, fees = [], {"a": {}, "b": {}}
    for when, day, o, qty, price in fills:
        fa, fb = fee_models(qty * price * ratio)
        fees["a"][day] = fees["a"].get(day, 0.0) + fa
        fees["b"][day] = fees["b"].get(day, 0.0) + fb
        sym, side = o.get("symbol", ""), o.get("side", "")
        if side == "buy":
            lots.setdefault(sym, []).append({"qty": qty, "left": qty, "price": price, "time": iso_utc(when), "day": day,
                                             "fee_a": fa, "fee_b": fb, "stop": stops.get(o.get("id"))})
            continue
        kind = o.get("type") or o.get("order_type") or ""
        remaining = qty
        while remaining > 1e-9 and lots.get(sym):
            lot = lots[sym][0]
            take = min(remaining, lot["left"])
            part_buy, part_sell = take / lot["qty"], take / qty
            stop = lot["stop"]
            reason = ("stop-loss" if kind in ("stop", "stop_limit", "trailing_stop")
                      else "sold at the open (kept overnight)" if day > lot["day"] else "sold before the close")
            trades.append({
                "symbol": sym, "date": day, "entry_time": lot["time"], "entry_price": lot["price"],
                "exit_time": iso_utc(when), "exit_price": price, "qty": round(take, 6), "stop": stop,
                "amount_real_usd": round(take * lot["price"] * ratio, 2),
                "pl_real_usd": round((price - lot["price"]) * take * ratio, 2),
                "pl_pct": price / lot["price"] - 1,
                "r": (price - lot["price"]) / (lot["price"] - stop) if stop and lot["price"] > stop else None,
                "fee_a_usd": round(lot["fee_a"] * part_buy + fa * part_sell, 4),
                "fee_b_usd": round(lot["fee_b"] * part_buy + fb * part_sell, 4),
                "reason": reason,
                "ai_reason": why.get((lot["day"], sym), ""),
            })
            lot["left"] -= take
            remaining -= take
            if lot["left"] <= 1e-9:
                lots[sym].pop(0)
    return trades, fees, {s: v for s, v in lots.items() if v}


def trade_stats(trades: list[dict]) -> dict:
    wins = [t for t in trades if t["pl_real_usd"] > 0]
    losses = [t for t in trades if t["pl_real_usd"] <= 0]
    gain, loss = sum(t["pl_real_usd"] for t in wins), -sum(t["pl_real_usd"] for t in losses)
    rs = [t["r"] for t in trades if t["r"] is not None]
    n = len(trades)
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": len(wins) / n if n else None,
        "avg_win_pct": sum(t["pl_pct"] for t in wins) / len(wins) if wins else None,
        "avg_loss_pct": sum(t["pl_pct"] for t in losses) / len(losses) if losses else None,
        "profit_factor": gain / loss if loss > 0 else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "pl_real_usd": round(gain - loss, 2),
        "fees_a_usd": round(sum(t["fee_a_usd"] for t in trades), 4),
        "fees_b_usd": round(sum(t["fee_b_usd"] for t in trades), 4),
        "stopped": sum(1 for t in trades if t["reason"] == "stop-loss"),
    }


def curve(history: dict, spy: dict[str, float], fees: dict, state: dict, today: str | None = None,
          equity_now: float | None = None, spy_now: float | None = None) -> list[dict]:
    """One point per trading day at real size: before costs, after each fee model (and Claude's cost), the same
    rules without Claude after each fee model, and SPY held."""
    real = config.DT_REAL_CAPITAL_USD
    by_day = {d["date"]: d for d in state.get("days", [])}
    ratio = real / state["start_equity"]
    values: dict[str, float] = {}
    for t, equity in zip(history.get("timestamp") or [], history.get("equity") or []):
        if equity:
            day = datetime.fromtimestamp(int(t), tz=NEW_YORK).strftime("%Y-%m-%d")
            if day >= state["start_date"]:
                values[day] = _f(equity)
    if today and equity_now and today >= state["start_date"]:
        values[today] = equity_now
    spy = dict(spy)
    if today and spy_now and today >= state["start_date"]:
        spy[today] = spy_now
    bench = state.get("bench_start_price") or 0
    spy_fee_a, spy_fee_b = fee_models(real)
    fa = fb = api = 0.0
    rules_a = rules_b = real
    last_spy, points = None, []
    for day in sorted(set(values) | set(fees["a"])):
        fa += fees["a"].get(day, 0.0)
        fb += fees["b"].get(day, 0.0)
        rec = by_day.get(day) or {}
        api += (rec.get("ai") or {}).get("cost_usd", 0.0)
        for t in (rec.get("rules_only") or {}).get("trades", []):
            rules_a, rules_b = _shadow_trade(rules_a, t["ret"], 0), _shadow_trade(rules_b, t["ret"], 1)
        last_spy = spy.get(day, last_spy)
        if day not in values:
            continue
        before = values[day] * ratio
        held = real * last_spy / bench if (last_spy and bench) else None
        open_day = bool(rec) and not rec.get("closed_at")  # replayed only at the close: no comparison before
        points.append({
            "date": day,
            "before": round(before, 2),
            "after_a": round(before - fa - api, 2),
            "after_b": round(before - fb - api, 2),
            "rules_a": None if open_day else round(rules_a, 2),
            "rules_b": None if open_day else round(rules_b, 2),
            "spy_a": round(held - spy_fee_a, 2) if held else None,
            "spy_b": round(held - spy_fee_b, 2) if held else None,
            "fees_a_cum": round(fa, 4),
            "fees_b_cum": round(fb, 4),
            "api_cum": round(api, 4),
        })
    return points


def _shadow_trade(value: float, ret: float, model: int) -> float:
    """One trade of the rules-without-Claude account: a fifth of its value, bought and sold with that fee model."""
    slot = value / config.DT_MAX_POSITIONS
    return value + slot * ret - fee_models(slot)[model] - fee_models(slot * (1 + ret))[model]


def settings_view() -> dict:
    return {
        "real_capital_usd": config.DT_REAL_CAPITAL_USD,
        "max_positions": config.DT_MAX_POSITIONS,
        "top_n": config.DT_TOP_N,
        "stop_atr": config.DT_STOP_ATR,
        "entry_end": config.DT_ENTRY_END,
        "close_before_min": config.DT_CLOSE_BEFORE_MIN,
        "fee_a": {"per_order_usd": config.FEE_PER_ORDER_USD, "rate": config.FEE_RATE},
        "fee_b": {"per_order_usd": config.DT_LOWCOST_FEE_PER_ORDER_USD, "rate": config.DT_LOWCOST_FEE_RATE},
        "min_price": config.DT_MIN_PRICE, "min_avg_volume": config.DT_MIN_AVG_VOLUME, "min_atr": config.DT_MIN_ATR,
        "ai_model": config.DT_AI_MODEL, "ai_effort": config.DT_AI_EFFORT, "api_budget_usd": config.DT_API_BUDGET_USD,
    }


def build_view(broker, state: dict | None, now: datetime) -> dict:
    """Everything the dashboard's day-trading section shows. Each part is fetched on its own."""
    payload = {"refreshed_at": iso_utc(now), "settings": settings_view(), "errors": [],
               "status": "running" if state else "waiting"}
    if not state:
        return payload
    from . import live  # shares the benchmark download with the stock side
    real = config.DT_REAL_CAPITAL_USD
    ratio = real / state["start_equity"]
    payload["start_date"] = state["start_date"]
    payload["api_cost_usd"] = round(state.get("api_cost_usd", 0.0), 4)

    def part(name, fn):
        try:
            return fn()
        except Exception as e:
            payload["errors"].append(f"{name} unavailable ({type(e).__name__})")
            return None

    clock = part("market clock", broker.clock) or {}
    payload["market_open"] = bool(clock.get("is_open"))
    today = ny_date(parse_ts(clock["timestamp"])) if clock.get("timestamp") else None
    account = part("account", broker.account)
    equity_now = _f(account.get("equity")) if account else None
    if account:
        last = _f(account.get("last_equity"))
        payload["account"] = {"value_real_usd": round(equity_now * ratio, 2),
                              "today_real_usd": round((equity_now - last) * ratio, 2) if last else None,
                              "today_pct": equity_now / last - 1 if last else None,
                              "cash_weight": _f(account.get("cash")) / equity_now if equity_now else None}
    rec = next((d for d in state["days"] if d["date"] == today), None) if today else None
    stops = {e["symbol"]: e["stop"] for e in (rec or {}).get("entries", [])}
    positions = part("positions", broker.positions_detail)
    if positions is not None:
        payload["positions"] = [{
            "symbol": p.get("symbol", ""), "qty": _f(p.get("qty")), "entry": _f(p.get("avg_entry_price")),
            "price": _f(p.get("current_price")), "stop": stops.get(p.get("symbol")),
            "value_real_usd": round(_f(p.get("market_value")) * ratio, 2),
            "pl_real_usd": round(_f(p.get("unrealized_pl")) * ratio, 2), "pl_pct": _f(p.get("unrealized_plpc")),
        } for p in positions if abs(_f(p.get("qty"))) > 1e-9]
    orders = part("trades", lambda: broker.filled_orders(state["start_date"] + "T00:00:00Z"))
    fees = {"a": {}, "b": {}}
    if orders is not None:
        trades, fees, _lots = trades_from_fills(orders, state)
        payload["stats"] = trade_stats(trades)
        payload["trades"] = trades[::-1][:config.DT_TRADES_SHOWN]
    intraday = part("today's curve", lambda: broker.portfolio_history())
    if intraday is not None:
        payload["intraday"] = [{"time": iso_utc(datetime.fromtimestamp(int(t), tz=timezone.utc)),
                                "value_real_usd": round(_f(e) * ratio, 2)}
                               for t, e in zip(intraday.get("timestamp") or [], intraday.get("equity") or []) if e]

    def daily():
        open_now = bool(clock.get("is_open"))
        spy_now = broker.latest_prices([config.BENCHMARK]).get(config.BENCHMARK) if open_now else None
        end = iso_utc(datetime.now(timezone.utc) - timedelta(minutes=20))
        pts = curve(broker.portfolio_history(timeframe="1D", start=state["start_date"]),
                    live._spy_closes(broker, state["start_date"], end), fees, state, today,
                    equity_now if open_now else None, spy_now)
        rf = state.get("risk_free_rate") or 0.0
        full = [p for p in pts if p["spy_a"] is not None]
        metrics = {
            "after_a": accounting.risk_metrics([p["after_a"] for p in full], real, rf, [p["spy_a"] for p in full]),
            "after_b": accounting.risk_metrics([p["after_b"] for p in full], real, rf, [p["spy_b"] for p in full]),
            "before": accounting.risk_metrics([p["before"] for p in full], real, rf, [p["spy_a"] for p in full]),
            "rules_a": accounting.risk_metrics([p["rules_a"] for p in full if p["rules_a"] is not None], real, rf,
                                               [p["spy_a"] for p in full if p["rules_a"] is not None]),
            "spy": accounting.risk_metrics([p["spy_a"] for p in full], real, rf),
        }
        return pts, metrics

    got = part("equity curve", daily)
    if got is not None:
        payload["curve"], payload["metrics"] = got
    payload["today"] = rec
    payload["days"] = [{k: d.get(k) for k in ("date", "status", "in_play", "eligible", "notes")}
                       | {"candidates": len(d.get("candidates", [])),
                          "entries": sum(1 for e in d.get("entries", []) if _counts(e)),
                          "ai_status": (d.get("ai") or {}).get("status"),
                          "ai_approved": (d.get("ai") or {}).get("approved"),
                          "rules_only_ret": (d.get("rules_only") or {}).get("ret")}
                       for d in state["days"]][::-1]
    return payload


def write_view(payload: dict, path: str = None) -> None:
    p = Path(path or config.DT_DATA_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1))


# --- starting the stock-picking run on time --------------------------------------------------------
def kick_daily_run(now: datetime, session: Session | None, path: str = "state/schedule.json",
                   dispatch=None) -> str:
    """GitHub starts the stock-picking run hours late (often after the close). While the market is open and
    that run has not decided today, start it now. At most twice a day, 45 minutes apart."""
    if session is None or not (session.open + timedelta(minutes=30) <= now < session.close - timedelta(minutes=45)):
        return ""
    swing = load_state(config.STATE_PATH)
    if swing and any(d["date"] == session.day for d in swing.get("days", [])):
        return ""
    p = Path(path)
    marks = json.loads(p.read_text()) if p.exists() else {}
    tries = [t for t in marks.get("daily_run_kicks", {}).get(session.day, [])]
    if len(tries) >= 2 or (tries and now - parse_ts(tries[-1]) < timedelta(minutes=45)):
        return ""
    if dispatch is None:
        def dispatch():
            subprocess.run(["gh", "workflow", "run", "daily-run.yml", "--ref", "main"], check=True,
                           capture_output=True, timeout=60)
    try:
        dispatch()
    except Exception as e:
        return f"could not start the stock-picking run ({type(e).__name__})"
    marks = {"daily_run_kicks": {session.day: tries + [iso_utc(now)]}}  # older days are dropped
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(marks, indent=1))
    return "started the stock-picking run"


# --- one run hands over to the next ------------------------------------------------------------------
def next_wake(state: dict | None, session: Session | None, now: datetime) -> datetime | None:
    """When a run should act again today, or None when nothing is left to do today."""
    if session is None or now >= session.close:
        return None
    prep_at = session.open - timedelta(minutes=config.DT_PREP_BEFORE_OPEN_MIN)
    if now < prep_at:
        return prep_at
    rec = next((d for d in (state or {}).get("days", []) if d["date"] == session.day), None)
    if rec is None:
        return session.scan_at if (state is not None and now < session.entry_end) else None
    if rec.get("closed_at"):
        return None
    if not rec.get("scanned_at") and now < session.entry_end:
        return max(now, session.scan_at)
    return max(now, session.close_at - timedelta(minutes=55))


def handover_time(must_end: datetime, deadline: datetime, now: datetime) -> datetime:
    """When to queue the next run so that it can still be running at `must_end`: as late as this run allows,
    never so early that the next run would end before `must_end` (runs handing over in a loop)."""
    latest = deadline - timedelta(minutes=5)
    earliest = must_end - timedelta(minutes=config.DT_MAX_RUN_MIN) + timedelta(minutes=5)
    return max(now, min(latest, max(earliest, now)))


def dispatch_next_run() -> str:
    """Queue the next day-trading run (it starts when this one ends); GitHub allows this with its own token."""
    if os.environ.get("GITHUB_ACTIONS") != "true" or not os.environ.get("GH_TOKEN"):
        return "next run not queued (not on GitHub)"
    try:
        subprocess.run(["gh", "workflow", "run", "day-trade.yml", "--ref", "main"], check=True,
                       capture_output=True, timeout=60)
    except Exception as e:
        return f"could not queue the next run ({type(e).__name__}): the outside timer starts one"
    return "next run queued"


# --- publishing from inside a long run -----------------------------------------------------------
def git_publish(message: str) -> None:
    """Commit the day-trading files and push, replaying on top of other runs' pushes."""
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)
    paths = [x for x in (config.DT_STATE_PATH, config.DT_DATA_PATH, "state/schedule.json") if Path(x).exists()]
    if paths:
        git("add", *paths)  # one missing path would make git add nothing at all
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for i in range(1, 6):
        if git("pull", "-q", "--rebase", "--autostash", "-X", "theirs").returncode == 0 and git("push", "-q").returncode == 0:
            return
        time.sleep(i * 5)
    print("Could not push the dashboard this time; the end of the run tries again.")


def main() -> None:
    from .alpaca import AlpacaPaper

    now = lambda: datetime.now(timezone.utc)  # noqa: E731
    key, secret = os.environ.get("DT_ALPACA_KEY_ID", ""), os.environ.get("DT_ALPACA_SECRET_KEY", "")
    publish_on = os.environ.get("DT_PUBLISH") == "1"
    swing_key, swing_secret = os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", "")

    clock_broker = AlpacaPaper(key, secret) if key and secret else (
        AlpacaPaper(swing_key, swing_secret) if swing_key and swing_secret else None)
    session = None
    if clock_broker is not None and os.environ.get("GH_TOKEN"):
        try:
            today = ny_date(parse_ts(clock_broker.clock()["timestamp"]))
            session = session_for(clock_broker, today)
            msg = kick_daily_run(now(), session)
            if msg:
                print(msg[0].upper() + msg[1:] + ".")
        except Exception as e:
            print(f"Stock-picking check skipped ({type(e).__name__}).")

    if not key or not secret:
        print("Day-trading keys missing (DT_ALPACA_KEY_ID / DT_ALPACA_SECRET_KEY): nothing to do.")
        if not Path(config.DT_DATA_PATH).exists():
            write_view(build_view(None, None, now()))
        return
    broker = AlpacaPaper(key, secret)
    if swing_key and swing_secret:  # it sells every position it finds: never on the stock-picking account
        same = key == swing_key or AlpacaPaper(swing_key, swing_secret).account().get("id") == broker.account().get("id")
        if same:
            raise SystemExit("The day-trading keys open the stock-picking account: use the keys of a second paper account.")

    def publish(state: dict) -> None:
        write_view(build_view(broker, state, now()))
        if publish_on:
            if session is not None:
                kick_daily_run(now(), session)
            git_publish(f"Day trading {now().strftime('%Y-%m-%dT%H:%MZ')}")

    from .dt_ai import review_candidates

    config.check()
    env = Env(now=now, sleep=time.sleep, publish=publish, review=review_candidates)
    env.started = now()
    deadline = env.started + timedelta(minutes=config.DT_MAX_RUN_MIN)
    state = load_state()
    for _ in range(12):  # each pass does what is due, then waits for the next step of the day
        state, message = run_tick(broker, state, env, save_state)
        print(message)
        today = ny_date(parse_ts(broker.clock()["timestamp"]))
        day_session = session_for(broker, today)
        wake = next_wake(state, day_session, now())
        if wake is None:
            break
        closing = wake >= day_session.close_at - timedelta(minutes=55)
        must_end = day_session.close_at + timedelta(minutes=8) if closing else wake + timedelta(minutes=5)
        if must_end > deadline:
            # This run cannot reach that step: wait until a fresh run could, then queue it and stop.
            wake = handover_time(must_end, deadline, now())
            hand_over = True
        else:
            hand_over = False
        while now() < wake:  # wait, keeping the dashboard fresh
            time.sleep(max(1.0, min((wake - now()).total_seconds(), config.DT_PUBLISH_EVERY_MIN * 60)))
            if state is not None and now() < wake:
                publish(state)
        if hand_over:
            if state is not None:
                publish(state)
            print(dispatch_next_run())
            break
    write_view(build_view(broker, state, now()))


if __name__ == "__main__":
    main()
