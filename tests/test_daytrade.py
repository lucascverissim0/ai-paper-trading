"""Offline tests of the day-trading side: a fake broker and a fake clock, no network, no install needed.

Run with:  python3 -m unittest tests.test_daytrade
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bot import config, daytrade, dt_ai, dt_backtest
from bot.alpaca import AlpacaError
from bot.daytrade import Candidate, DailyStats, Env, Session, at_ny, parse_ts

DAY = "2026-11-20"  # a Friday, New York winter time (UTC-5)


def ny(hhmm: str, day: str = DAY) -> datetime:
    return at_ny(day, hhmm)


class FakeClock:
    def __init__(self, start: datetime):
        self.t = start.astimezone(timezone.utc)
        self.hooks = []
        self.sleeps = 0

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps += 1
        self.t += timedelta(seconds=seconds)
        for hook in self.hooks:
            hook()


class Spec:
    """One fake stock: daily figures, its usual first-5-minute volume, today's first bar and a price path."""

    def __init__(self, price, avg_vol, atr, first_hist_vol, first_bar=None, path=None, name="Fake Co"):
        self.price, self.avg_vol, self.atr, self.first_hist_vol = price, avg_vol, atr, first_hist_vol
        self.first_bar = first_bar  # (o, h, l, c, v) today
        self.path = path or []      # [(New York "HH:MM", price)], the price from that minute on
        self.name = name

    def price_at(self, t: datetime) -> float:
        p = self.first_bar[3] if self.first_bar else self.price
        for hhmm, value in self.path:
            if t >= ny(hhmm, daytrade.ny_date(t)):
                p = value
        return p


def standard_specs() -> dict[str, Spec]:
    return {
        # in play, rose in the first 5 minutes, breaks out at 9:40, closes higher
        "AAA": Spec(50, 2_000_000, 2.0, 10_000, (50.0, 51.0, 49.8, 50.8, 50_000), [("09:40", 51.10), ("10:30", 52.0)]),
        # in play but fell in the first 5 minutes: never bought (long only)
        "BBB": Spec(40, 3_000_000, 1.5, 10_000, (40.0, 40.2, 38.0, 38.5, 80_000)),
        # rose but traded less than usual: not in play
        "CCC": Spec(60, 2_000_000, 2.0, 10_000, (60.0, 61.0, 59.9, 60.9, 5_000), [("09:50", 62.0)]),
        # too little daily volume: never eligible
        "DDD": Spec(30, 500_000, 1.0, 1_000, (30.0, 31.0, 29.9, 30.9, 90_000)),
        # breaks out with a jump far past its high: skipped, not chased
        "EEE": Spec(30, 1_500_000, 1.0, 5_000, (30.0, 30.5, 29.9, 30.4, 15_000), [("09:45", 31.0)]),
        # rose but never breaks its high
        "FFF": Spec(20, 1_200_000, 0.8, 4_000, (20.0, 20.4, 19.9, 20.3, 8_000), [("09:36", 20.1)]),
        # under $5 at the open
        "GGG": Spec(4.5, 9_000_000, 0.6, 50_000, (4.5, 4.8, 4.4, 4.7, 500_000)),
        # leveraged fund: left out of the universe
        "LLL": Spec(80, 9_000_000, 4.0, 50_000, (80.0, 82.0, 79.0, 81.5, 900_000), name="Direxion Daily Semis Bull 3X Shares"),
        "SPY": Spec(600, 60_000_000, 6.0, 2_000_000, None, [("09:30", 601.0)], name="SPDR S&P 500"),
    }


class DTBroker:
    def __init__(self, clock: FakeClock, specs: dict[str, Spec] | None = None):
        self.clock_ = clock
        self.specs = specs or standard_specs()
        self.holidays: set[str] = set()
        self.half_days: dict[str, str] = {}
        self.cash_ = 100_000.0
        self.pos: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.used_ids: set[str] = set()
        self.n = 0
        self.refuse_gtc = False
        self.reject = set()
        self.bar_requests: list[tuple] = []
        clock.hooks.append(self.process_stops)

    # -- calendar and clock
    def calendar(self, start, end):
        d, out = date.fromisoformat(start), []
        while d <= date.fromisoformat(end):
            if d.weekday() < 5 and d.isoformat() not in self.holidays:
                out.append({"date": d.isoformat(), "open": "09:30", "close": self.half_days.get(d.isoformat(), "16:00")})
            d += timedelta(days=1)
        return out

    def _session(self, day):
        rows = self.calendar(day, day)
        return (ny(rows[0]["open"], day), ny(rows[0]["close"], day)) if rows else None

    def clock(self):
        now = self.clock_.now()
        day = daytrade.ny_date(now)
        s = self._session(day)
        local = now.astimezone(daytrade.NEW_YORK)
        offset = local.strftime("%z")
        stamp = local.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123" + offset[:3] + ":" + offset[3:]  # nanoseconds
        return {"timestamp": stamp, "is_open": bool(s and s[0] <= now < s[1])}

    # -- data
    def assets(self):
        return [{"symbol": s, "name": sp.name, "exchange": "NYSE", "status": "active", "tradable": True}
                for s, sp in self.specs.items()]

    def bars(self, symbols, timeframe, start, end, feed, adjustment="raw", pause=0.35):
        self.bar_requests.append((timeframe, tuple(symbols), start, feed))
        out = {}
        if timeframe == "1Day":
            days = [r["date"] for r in self.calendar(start[:10], end[:10]) if r["date"] < end[:10]]
            for s in symbols:
                sp = self.specs.get(s)
                if sp:
                    out[s] = [{"t": d + "T05:00:00Z", "o": sp.price, "h": sp.price + sp.atr / 2,
                               "l": sp.price - sp.atr / 2, "c": sp.price, "v": sp.avg_vol} for d in days]
        elif timeframe == "5Min":
            t0 = parse_ts(start)
            today = daytrade.ny_date(self.clock_.now())
            for s in symbols:
                sp = self.specs.get(s)
                if not sp:
                    continue
                if daytrade.ny_date(t0) == today:
                    if sp.first_bar and self.clock_.now() >= t0 + timedelta(minutes=5):
                        o, h, low, c, v = sp.first_bar
                        out[s] = [{"t": start, "o": o, "h": h, "l": low, "c": c, "v": v}]
                else:
                    out[s] = [{"t": start, "o": sp.price, "h": sp.price, "l": sp.price, "c": sp.price, "v": sp.first_hist_vol}]
        elif timeframe == "1Min":
            t, stop = parse_ts(start), min(parse_ts(end), self.clock_.now())
            while t < stop:
                for s in symbols:
                    p = self.specs[s].price_at(t)
                    out.setdefault(s, []).append({"t": daytrade.iso_utc(t), "o": p, "h": p, "l": p, "c": p, "v": 100})
                t += timedelta(minutes=1)
        return out

    def daily_bars(self, symbols, start, end, feed):
        return {s: [(r["t"][:10], r["c"], r["v"]) for r in rows] for s, rows in self.bars(symbols, "1Day", start, end, feed).items()}

    def latest_prices(self, symbols):
        now = self.clock_.now()
        return {s: self.specs[s].price_at(now) for s in symbols if s in self.specs}

    # -- account
    def price(self, s):
        return self.specs[s].price_at(self.clock_.now())

    def equity(self):
        return self.cash_ + sum(p["qty"] * self.price(s) for s, p in self.pos.items())

    def account(self):
        return {"equity": str(self.equity()), "last_equity": "100000", "cash": str(self.cash_)}

    def cash(self):
        return self.cash_

    def positions_detail(self):
        return [{"symbol": s, "qty": str(p["qty"]), "avg_entry_price": str(p["entry"]), "current_price": str(self.price(s)),
                 "market_value": str(p["qty"] * self.price(s)), "unrealized_pl": str(p["qty"] * (self.price(s) - p["entry"])),
                 "unrealized_plpc": str(self.price(s) / p["entry"] - 1)} for s, p in self.pos.items()]

    def portfolio_history(self, period="1D", timeframe="15Min", start=None):
        t = int(ny("00:00", daytrade.ny_date(self.clock_.now())).timestamp())
        return {"timestamp": [t], "equity": [self.equity()]}

    # -- orders
    def _id(self):
        self.n += 1
        return f"o{self.n}"

    def _fill(self, symbol, side, qty, price, kind):
        if side == "buy":
            assert qty * price <= self.cash_ + 1e-6, "bought with money it does not have"
            self.cash_ -= qty * price
            p = self.pos.setdefault(symbol, {"qty": 0, "entry": price})
            p["entry"] = (p["entry"] * p["qty"] + price * qty) / (p["qty"] + qty)
            p["qty"] += qty
        else:
            assert qty <= self.pos[symbol]["qty"] + 1e-9, "sold shares it does not hold"
            self.cash_ += qty * price
            self.pos[symbol]["qty"] -= qty
            if self.pos[symbol]["qty"] <= 1e-9:
                del self.pos[symbol]
        oid = self._id()
        self.orders[oid] = {"id": oid, "symbol": symbol, "side": side, "type": kind, "status": "filled",
                            "filled_qty": str(qty), "filled_avg_price": str(price),
                            "filled_at": daytrade.iso_utc(self.clock_.now())}
        return self.orders[oid]

    def oto_market_buy(self, symbol, qty, stop_price, client_order_id, time_in_force="gtc"):
        if client_order_id in self.used_ids:
            raise AlpacaError("POST /v2/orders failed with 422: client_order_id must be unique")
        if self.refuse_gtc and time_in_force == "gtc":
            raise AlpacaError("POST /v2/orders failed with 422: invalid time_in_force for oto order")
        self.used_ids.add(client_order_id)
        if symbol in self.reject:
            oid = self._id()
            self.orders[oid] = {"id": oid, "symbol": symbol, "side": "buy", "type": "market", "status": "rejected"}
            return self.orders[oid]
        parent = self._fill(symbol, "buy", qty, self.price(symbol), "market")
        parent["client_order_id"] = client_order_id
        leg = self._id()
        self.orders[leg] = {"id": leg, "symbol": symbol, "side": "sell", "type": "stop", "status": "new",
                            "qty": str(qty), "stop_price": stop_price, "tif": time_in_force}
        return parent

    def wait_final(self, order_id, timeout_s=60):
        return self.orders[order_id]

    def open_orders(self):
        return [o for o in self.orders.values() if o["status"] in ("new", "accepted", "held")]

    def cancel_order(self, order_id):
        self.orders[order_id]["status"] = "canceled"

    def close_position(self, symbol):
        assert not any(o["symbol"] == symbol for o in self.open_orders()), "shares still held by a stop order"
        return self._fill(symbol, "sell", self.pos[symbol]["qty"], self.price(symbol), "market")

    def filled_orders(self, after_iso):
        return sorted([o for o in self.orders.values() if o["status"] == "filled" and o["filled_at"] >= after_iso],
                      key=lambda o: o["filled_at"])

    def process_stops(self):
        s = self._session(daytrade.ny_date(self.clock_.now()))
        if not s or not s[0] <= self.clock_.now() < s[1]:
            return None  # stop orders only trigger during market hours
        for o in list(self.open_orders()):
            if o["type"] == "stop" and o["symbol"] in self.pos and self.price(o["symbol"]) <= o["stop_price"]:
                o["status"] = "replaced"
                filled = self._fill(o["symbol"], "sell", float(o["qty"]), o["stop_price"], "stop")
                o.update({k: filled[k] for k in ("filled_qty", "filled_avg_price", "filled_at")}, status="filled")
                del self.orders[filled["id"]]
        return None


class Run:
    """Runs run_tick against the fakes and keeps the state as the real files would."""

    def __init__(self, broker: DTBroker, clock: FakeClock):
        self.broker, self.clock, self.state, self.publishes = broker, clock, None, 0
        self.review = None

    def tick(self, at: datetime):
        if at > self.clock.now():
            self.clock.t = at.astimezone(timezone.utc)
        env = Env(now=self.clock.now, sleep=self.clock.sleep, publish=self._publish, pause=0, review=self.review)
        self.state, msg = daytrade.run_tick(self.broker, self.state, env, lambda s: None)
        return msg

    def _publish(self, state):
        self.publishes += 1

    def day(self, d: str = DAY) -> dict:
        return next(x for x in self.state["days"] if x["date"] == d)

    def cand(self, symbol: str, d: str = DAY) -> dict:
        return next(c for c in self.day(d)["candidates"] if c["symbol"] == symbol)


def new_run(start: str = "09:00", day: str = DAY, specs=None) -> Run:
    clock = FakeClock(ny(start, day))
    return Run(DTBroker(clock, specs), clock)


# ------------------------------------------------------------------------------------------
class RulesTest(unittest.TestCase):
    def test_atr_and_volume_by_hand(self):
        bars = [{"t": f"d{i}", "o": 10, "h": 11, "l": 9, "c": 10, "v": 100} for i in range(15)]
        bars[-1] = {"t": "x", "o": 10, "h": 14, "l": 10, "c": 13, "v": 1500}  # true range 4 (high - previous close)
        st = daytrade.daily_stats("X", bars)
        self.assertAlmostEqual(st.atr, (13 * 2 + 4) / 14)
        self.assertAlmostEqual(st.avg_volume, (13 * 100 + 1500) / 14)
        self.assertEqual(st.close, 13)
        self.assertIsNone(daytrade.daily_stats("X", bars[:14]))

    def test_selection_in_play_long_only(self):
        stats = {s: DailyStats(s, 50, 2e6, 2.0) for s in ("A", "B", "C", "D", "E")}
        stats["E"] = DailyStats("E", 50, 2e6, 0.3)  # moves too little
        first = {"A": {"o": 50, "h": 51, "l": 49, "c": 50.5, "v": 300},
                 "B": {"o": 50, "h": 51, "l": 49, "c": 49.5, "v": 900},   # fell: in play, not bought
                 "C": {"o": 50, "h": 51, "l": 49, "c": 50.5, "v": 50},    # under its usual volume
                 "D": {"o": 4.9, "h": 5.2, "l": 4.8, "c": 5.1, "v": 900},  # opened under $5
                 "E": {"o": 50, "h": 51, "l": 49, "c": 50.5, "v": 900}}
        avg = dict.fromkeys(first, 100.0)
        cands, in_play = daytrade.select(first, avg, stats)
        self.assertEqual(in_play, 2)
        self.assertEqual([(c.symbol, c.rank, c.rv) for c in cands], [("A", 2, 3.0)])

    def test_top_twenty_only(self):
        stats = {f"S{i:02d}": DailyStats(f"S{i:02d}", 50, 2e6, 2.0) for i in range(30)}
        first = {s: {"o": 50, "h": 51, "l": 49, "c": 50.5, "v": 100 + i} for i, s in enumerate(sorted(stats))}
        cands, in_play = daytrade.select(first, dict.fromkeys(stats, 100.0), stats)
        self.assertEqual(in_play, config.DT_TOP_N)
        self.assertEqual(cands[0].symbol, "S29")
        self.assertNotIn("S09", [c.symbol for c in cands])

    def test_entry_stop_and_size(self):
        self.assertEqual(daytrade.entry_signal(10.0, 1.0, 10.0), "wait")
        self.assertEqual(daytrade.entry_signal(10.0, 1.0, 10.05), "enter")
        self.assertEqual(daytrade.entry_signal(10.0, 1.0, 10.11), "skip")
        self.assertEqual(daytrade.stop_for(51.10, 2.0), 50.90)
        self.assertEqual(daytrade.shares_for(20_000, 100_000, 51.10), 391)
        self.assertEqual(daytrade.shares_for(20_000, 5_000, 51.10), 95)  # never more than the cash
        self.assertEqual(daytrade.shares_for(20_000, -1, 51.10), 0)

    def test_fee_models(self):
        a, b = daytrade.fee_models(200.0)
        self.assertAlmostEqual(a, 1.0 + 0.2)
        self.assertAlmostEqual(b, 0.1)
        self.assertEqual(daytrade.fee_models(0), (0.0, 0.0))

    def test_times(self):
        self.assertEqual(parse_ts("2026-11-20T09:35:01.123456789-05:00"), datetime(2026, 11, 20, 14, 35, 1, 123456, timezone.utc))
        self.assertEqual(parse_ts("2026-11-20T14:30:00Z"), ny("09:30"))
        winter = Session(DAY, ny("09:30"), ny("16:00"))
        summer = Session("2026-07-15", ny("09:30", "2026-07-15"), ny("16:00", "2026-07-15"))
        self.assertEqual(daytrade.iso_utc(winter.open), "2026-11-20T14:30:00Z")
        self.assertEqual(daytrade.iso_utc(summer.open), "2026-07-15T13:30:00Z")
        self.assertEqual(winter.entry_end, ny("12:00"))
        self.assertEqual(winter.close_at, ny("15:50"))
        half = Session(DAY, ny("09:30"), ny("13:00"))
        self.assertEqual(half.entry_end, ny("12:00"))
        self.assertEqual(half.close_at, ny("12:50"))


class DayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_state_path = config.STATE_PATH
        config.STATE_PATH = os.path.join(self.tmp.name, "ledger.json")

    def tearDown(self):
        config.STATE_PATH = self.old_state_path
        self.tmp.cleanup()

    def test_full_day(self):
        r = new_run("09:00")
        r.tick(ny("09:00"))
        rec = r.day()
        self.assertEqual(rec["in_play"], 4)  # AAA BBB EEE FFF; CCC traded under its usual volume
        states = {c["symbol"]: c["state"] for c in rec["candidates"]}
        self.assertEqual(states, {"AAA": "entered", "EEE": "skipped", "FFF": "no breakout"})
        self.assertNotIn("LLL", states)
        self.assertEqual(rec["status"], "holding")
        self.assertEqual(r.state["start_equity"], 100_000.0)
        self.assertEqual(r.state["bench_start_price"], 601.0)
        (entry,) = rec["entries"]
        self.assertEqual((entry["symbol"], entry["qty"], entry["stop"], entry["fill_price"]), ("AAA", 391, 50.9, 51.10))
        self.assertEqual(r.clock.now(), ny("12:00"))  # waited for breakouts until noon, then stopped
        stop_legs = [o for o in r.broker.open_orders() if o["type"] == "stop"]
        self.assertEqual([(o["symbol"], o["stop_price"], o["tif"]) for o in stop_legs], [("AAA", 50.9, "gtc")])

        self.assertEqual(r.tick(ny("12:30")), "Nothing due now.")  # nothing to do between noon and the close
        self.assertEqual(len(r.day()["entries"]), 1)

        r.tick(ny("15:00"))
        rec = r.day()
        self.assertEqual(r.clock.now().astimezone(daytrade.NEW_YORK).strftime("%H:%M"), "15:50")
        self.assertEqual(rec["status"], "closed")
        self.assertEqual(r.broker.pos, {})
        self.assertEqual(r.broker.open_orders(), [])
        trades, fees, lots = daytrade.trades_from_fills(r.broker.filled_orders("2026-01-01"), r.state)
        (t,) = trades
        self.assertEqual(t["reason"], "sold before the close")
        self.assertAlmostEqual(t["pl_real_usd"], round((52.0 - 51.10) * 391 * 0.01, 2))
        self.assertAlmostEqual(t["r"], (52.0 - 51.10) / 0.20)
        buy, sell = 391 * 51.10 * 0.01, 391 * 52.0 * 0.01
        self.assertAlmostEqual(t["fee_a_usd"], round(2 + 0.001 * (buy + sell), 4))
        self.assertAlmostEqual(t["fee_b_usd"], round(0.0005 * (buy + sell), 4))
        self.assertAlmostEqual(fees["a"][DAY], 2 + 0.001 * (buy + sell))
        self.assertEqual(lots, {})
        self.assertEqual(r.tick(ny("15:55")), "Nothing due now.")
        r.tick(ny("16:30"))
        self.assertNotIn("close missed", r.day()["status"])

    def test_stop_loss_then_no_waiting_at_the_close(self):
        specs = standard_specs()
        specs["AAA"].path = [("09:40", 51.10), ("10:15", 50.0)]  # falls through its stop at 50.90
        r = new_run("09:00", specs=specs)
        r.tick(ny("09:00"))
        self.assertEqual(r.broker.pos, {})
        r.tick(ny("15:00"))
        self.assertEqual(r.clock.now(), ny("15:00"))  # nothing held: no need to wait for 15:50
        (t,) = daytrade.trades_from_fills(r.broker.filled_orders("2026-01-01"), r.state)[0]
        self.assertEqual(t["reason"], "stop-loss")
        self.assertAlmostEqual(t["r"], -1.0)

    def test_slots_and_no_borrowing(self):
        specs = standard_specs()
        for i in range(7):  # more breakouts than places
            specs[f"X{i}"] = Spec(40, 2e6, 2.0, 1_000, (40.0, 41.0, 39.9, 40.9, 9_000 + i), [("09:41", 41.05)])
        r = new_run("09:00", specs=specs)
        r.tick(ny("09:00"))
        rec = r.day()
        self.assertEqual(sum(1 for c in rec["candidates"] if c["state"] == "entered"), config.DT_MAX_POSITIONS)
        self.assertTrue(any(c["state"] == "not taken" for c in rec["candidates"]))
        self.assertGreaterEqual(r.broker.cash_, 0)  # never borrowed

    def test_never_buys_twice(self):
        old = config.DT_MAX_RUN_MIN
        config.DT_MAX_RUN_MIN = 50  # the morning run stops at 9:48, AAA bought at 9:40
        try:
            r = new_run("09:00")
            r.tick(ny("09:00"))
        finally:
            config.DT_MAX_RUN_MIN = old
        self.assertEqual(r.cand("AAA")["state"], "entered")
        r.cand("AAA")["state"] = "waiting"   # the ledger lost track of the purchase...
        r.day()["entries"] = []
        before = len(r.broker.orders)
        r.tick(ny("10:00"))                  # ...the next run still does not buy it again (already held)
        self.assertEqual(len(r.broker.orders), before)
        self.assertEqual(r.broker.pos["AAA"]["qty"], 391)
        # and Alpaca itself refuses a second order with the same id
        c = {"symbol": "AAA", "atr": 2.0, "state": "waiting", "detail": ""}
        rec = {"date": DAY, "slot_usd": 20_000, "entries": []}
        daytrade._enter(r.broker, c, 52.0, rec, r.clock.now(), lambda: None)
        self.assertEqual(c["state"], "error")
        self.assertIn("unique", rec["entries"][0]["status"])
        self.assertEqual(len(r.broker.orders), before)

    def test_rejected_order_uses_no_place(self):
        r = new_run("09:00")
        r.broker.reject.add("AAA")
        r.tick(ny("09:00"))
        rec = r.day()
        (entry,) = rec["entries"]
        self.assertEqual(entry["status"], "rejected")
        self.assertFalse(daytrade._counts(entry))
        self.assertEqual(next(c for c in rec["candidates"] if c["symbol"] == "AAA")["state"], "error")
        self.assertNotIn("AAA", r.broker.pos)
        self.assertNotEqual(rec["status"], "holding")

    def test_refused_gtc_is_sent_as_day_order(self):
        r = new_run("09:00")
        r.broker.refuse_gtc = True
        r.tick(ny("09:00"))
        (entry,) = r.day()["entries"]
        self.assertEqual(entry["status"], "filled")
        self.assertEqual([o["tif"] for o in r.broker.open_orders()], ["day"])

    def test_leftover_sold_at_the_next_open(self):
        r = new_run("09:00")
        r.tick(ny("09:00"))           # buys AAA, then no closing run that day
        r.tick(ny("16:20"))
        self.assertEqual(r.day()["status"], "close missed")
        self.assertTrue(any("kept overnight" in n for n in r.day()["notes"]))
        r.broker.specs["AAA"].path = [("00:00", 52.0)]  # Monday: above its stop, too high to buy again
        r.tick(ny("09:00", "2026-11-23"))  # Monday: sold as soon as the market opens, before the scan
        monday = r.day("2026-11-23")
        self.assertEqual([(x["symbol"], x["status"]) for x in monday["leftovers"]], [("AAA", "filled")])
        trades = daytrade.trades_from_fills(r.broker.filled_orders("2026-01-01"), r.state)[0]
        self.assertEqual(trades[0]["reason"], "sold at the open (kept overnight)")

    def test_late_first_run_marks_missed_breakouts(self):
        r = new_run("10:30")
        r.tick(ny("10:30"))
        rec = r.day()
        self.assertEqual(r.cand("AAA")["state"], "missed")
        self.assertTrue(any("scanned late" in n for n in rec["notes"]))
        self.assertEqual(rec["entries"], [])

    def test_run_after_noon_does_not_scan(self):
        r = new_run("09:00")
        r.tick(ny("09:00"))
        r.tick(ny("15:00"))
        r.tick(ny("12:30", "2026-11-23"))
        self.assertEqual(r.day("2026-11-23")["status"], "no scan")
        self.assertFalse(any(req[0] == "5Min" and "2026-11-23" in req[2] for req in r.broker.bar_requests))

    def test_half_day_closes_at_12_50(self):
        r = new_run("09:00")
        r.broker.half_days[DAY] = "13:00"
        r.tick(ny("09:00"))
        self.assertEqual(r.day()["status"], "closed")
        self.assertEqual(r.clock.now(), ny("12:50"))
        self.assertEqual(r.broker.pos, {})

    def test_refuses_an_account_that_already_holds_shares(self):
        r = new_run("09:00")
        r.broker._fill("BBB", "buy", 10, 40.0, "market")  # someone else's position
        msg = r.tick(ny("09:00"))
        self.assertIn("Refused to start", msg)
        self.assertIsNone(r.state)
        self.assertIn("BBB", r.broker.pos)
        self.assertEqual([o for o in r.broker.orders.values() if o["side"] == "sell"], [])

    def test_holiday_and_too_early(self):
        r = new_run("09:00")
        r.broker.holidays.add(DAY)
        self.assertIn("No trading session", r.tick(ny("09:00")))
        r = new_run("08:00")
        self.assertIn("Too early", r.tick(ny("08:00")))
        self.assertIsNone(r.state)

    def test_view_numbers(self):
        r = new_run("09:00")
        r.tick(ny("09:00"))
        r.tick(ny("15:00"))
        view = daytrade.build_view(r.broker, r.state, r.clock.now())
        self.assertEqual(view["errors"], [])
        self.assertEqual(view["stats"]["trades"], 1)
        self.assertEqual(view["stats"]["win_rate"], 1.0)
        (p,) = view["curve"]
        self.assertAlmostEqual(p["before"], round(r.broker.equity() * 0.01, 2))
        self.assertAlmostEqual(p["after_a"], round(p["before"] - p["fees_a_cum"], 2))
        self.assertAlmostEqual(p["after_b"], round(p["before"] - p["fees_b_cum"], 2))
        self.assertGreater(p["after_b"], p["after_a"])
        json.dumps(view)  # the page can read it
        self.assertEqual(daytrade.build_view(None, None, r.clock.now())["status"], "waiting")


def fake_review(picks: dict, status: str = "ok", cost: float = 0.05):
    """Stands in for Claude: picks is symbol -> (trade, score)."""
    seen = {}

    def review(broker, cands, prep, today, now, first, spent):
        seen["symbols"] = [c.symbol for c in cands]
        seen["spent"] = spent
        out = {s: {"trade": t, "score": sc, "reason": f"reason {s}"} for s, (t, sc) in picks.items()}
        return dt_ai.Review(status, out if status == "ok" else {}, "summary", cost, model="test-model")
    review.seen = seen
    return review


class ClaudeTest(unittest.TestCase):
    def test_claude_turns_down_and_orders(self):
        r = new_run("09:00")
        r.review = fake_review({"AAA": (False, 2), "EEE": (True, 8), "FFF": (True, 9)})
        r.tick(ny("09:00"))
        rec = r.day()
        self.assertEqual(sorted(r.review.seen["symbols"]), ["AAA", "EEE", "FFF"])
        self.assertEqual(r.cand("AAA")["state"], "ai pass")
        self.assertEqual(r.cand("AAA")["detail"], "Claude: reason AAA")
        self.assertEqual((r.cand("FFF")["priority"], r.cand("EEE")["priority"]), (1, 2))
        self.assertEqual(rec["entries"], [])
        self.assertNotIn("AAA", r.broker.pos)
        self.assertEqual((rec["ai"]["status"], rec["ai"]["approved"], rec["ai"]["rated"]), ("ok", 2, 3))
        self.assertAlmostEqual(r.state["api_cost_usd"], 0.05)
        r.tick(ny("15:00"))
        rec = r.day()
        self.assertIsNotNone(rec["closed_at"])
        # the same rules without Claude would have bought AAA: replayed at the close
        self.assertEqual([t["symbol"] for t in rec["rules_only"]["trades"]], ["AAA"])

    def test_unrated_candidate_is_not_bought(self):
        r = new_run("09:00")
        r.review = fake_review({"EEE": (True, 5)})
        r.tick(ny("09:00"))
        self.assertEqual(r.cand("AAA")["state"], "ai pass")
        self.assertIn("not rated", r.cand("AAA")["detail"])

    def test_claude_passes_on_all(self):
        r = new_run("09:00")
        r.review = fake_review({"AAA": (False, 1), "EEE": (False, 1), "FFF": (False, 1)})
        r.tick(ny("09:00"))
        self.assertEqual(r.day()["status"], "ai passed")
        self.assertEqual(r.day()["entries"], [])

    def test_no_answer_means_rules_alone(self):
        for review in (fake_review({}, status="api_error", cost=0.0),
                       lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))):
            r = new_run("09:00")
            r.review = review
            r.tick(ny("09:00"))
            self.assertEqual(r.cand("AAA")["state"], "entered")
            self.assertTrue(any("rules alone" in n for n in r.day()["notes"]))
            self.assertEqual(r.day()["ai"]["status"], "api_error")

    def test_claude_cost_and_rules_line_in_the_view(self):
        r = new_run("09:00")
        r.review = fake_review({"AAA": (False, 2), "EEE": (True, 8), "FFF": (True, 9)}, cost=0.25)
        r.tick(ny("09:00"))
        r.tick(ny("15:00"))
        view = daytrade.build_view(r.broker, r.state, r.clock.now())
        (p,) = view["curve"]
        self.assertAlmostEqual(p["after_a"], round(p["before"] - p["fees_a_cum"] - 0.25, 2))
        self.assertEqual(view["api_cost_usd"], 0.25)
        (t,) = r.day()["rules_only"]["trades"]
        slot = 1000 / 5
        expected = 1000 + slot * t["ret"] - (1 + 0.001 * slot) - (1 + 0.001 * slot * (1 + t["ret"]))
        self.assertAlmostEqual(p["rules_a"], round(expected, 2))
        self.assertIn("rules_a", view["metrics"])
        json.dumps(view)

    def test_answer_cleaning(self):
        picks, summary = dt_ai.clean({"candidates": [
            {"symbol": "aaa", "trade": True, "score": 14, "reason": "x" * 500},
            {"symbol": "AAA", "trade": False, "score": 1, "reason": "second answer ignored"},
            {"symbol": "ZZZ", "trade": True, "score": 5, "reason": "not a candidate"},
            {"symbol": "BBB", "trade": "yes", "score": "n/a", "reason": ""}], "summary": "ok"}, {"AAA", "BBB"})
        self.assertEqual(picks["AAA"]["score"], 10)
        self.assertEqual(len(picks["AAA"]["reason"]), dt_ai.MAX_REASON)
        self.assertTrue(picks["AAA"]["trade"])
        self.assertEqual(picks["BBB"], {"trade": False, "score": 1, "reason": ""})  # only a real true buys
        self.assertNotIn("ZZZ", picks)
        self.assertEqual(dt_ai.clean("garbage", {"AAA"}), ({}, ""))

    def test_prompt_and_budget(self):
        c = Candidate("AAA", 1, 3.5, 50.0, 51.0, 49.8, 50.9, 1e6, 1.2)
        prompt = dt_ai.prompt_for(DAY, [c], {"AAA": 48.0}, {"AAA": "Alpha Inc"}, {"AAA": [
            {"created_at": "2026-11-16T12:00:00Z", "headline": "Alpha beats", "summary": "big quarter"}]}, "SPY flat.")
        self.assertIn("Alpha Inc", prompt)
        self.assertIn("gap from yesterday's close +4.2%", prompt)
        self.assertIn("Alpha beats", prompt)
        r = dt_ai.ask(prompt, {"AAA"}, spent_usd=config.DT_API_BUDGET_USD)
        self.assertEqual(r.status, "over_budget")  # never calls Claude past the budget

    def test_news_kept_recent_and_per_symbol(self):
        now = datetime(2026, 11, 16, 14, 36, tzinfo=timezone.utc)

        class NewsBroker:
            def news(self, symbols, limit=50):
                return [{"created_at": "2026-11-16T13:00:00Z", "headline": "fresh", "symbols": ["AAA", "XXX"]},
                        {"created_at": "2026-11-01T13:00:00Z", "headline": "old", "symbols": ["AAA"]},
                        {"created_at": "2026-11-16T15:00:00Z", "headline": "after now", "symbols": ["AAA"]}]
        out = dt_ai.news_by_symbol(NewsBroker(), ["AAA", "BBB"], now)
        self.assertEqual([n["headline"] for n in out["AAA"]], ["fresh"])
        self.assertEqual(out["BBB"], [])


class HandOverTest(unittest.TestCase):
    def test_next_wake(self):
        s = Session(DAY, at_ny(DAY, "09:30"), at_ny(DAY, "16:00"))
        self.assertEqual(daytrade.next_wake(None, s, ny("07:00")), ny("08:45"))
        self.assertIsNone(daytrade.next_wake(None, s, ny("09:00")))   # the run itself scans; nothing to resume
        self.assertIsNone(daytrade.next_wake(None, None, ny("09:00")))
        state = {"days": []}
        self.assertEqual(daytrade.next_wake(state, s, ny("09:00")), s.scan_at)
        self.assertIsNone(daytrade.next_wake(state, s, ny("13:00")))
        state = {"days": [{"date": DAY, "scanned_at": "x"}]}
        self.assertEqual(daytrade.next_wake(state, s, ny("12:00")), ny("14:55"))
        self.assertEqual(daytrade.next_wake(state, s, ny("15:10")), ny("15:10"))
        state["days"][0]["closed_at"] = "y"
        self.assertIsNone(daytrade.next_wake(state, s, ny("15:10")))
        self.assertIsNone(daytrade.next_wake({"days": [{"date": DAY}]}, s, ny("16:00")))

    def test_dispatch_only_on_github(self):
        old = os.environ.pop("GITHUB_ACTIONS", None)
        try:
            self.assertIn("not on GitHub", daytrade.dispatch_next_run())
        finally:
            if old is not None:
                os.environ["GITHUB_ACTIONS"] = old


class KickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "schedule.json")
        self.old = config.STATE_PATH
        config.STATE_PATH = os.path.join(self.tmp.name, "ledger.json")
        self.session = Session(DAY, ny("09:30"), ny("16:00"))
        self.calls = 0

    def tearDown(self):
        config.STATE_PATH = self.old
        self.tmp.cleanup()

    def kick(self, hhmm):
        def dispatch():
            self.calls += 1
        return daytrade.kick_daily_run(ny(hhmm), self.session, self.path, dispatch)

    def test_only_when_open_not_yet_decided_twice_at_most(self):
        self.assertEqual(self.kick("09:45"), "")          # too soon after the open
        self.assertTrue(self.kick("10:00"))
        self.assertEqual(self.kick("10:30"), "")          # 45 minutes apart
        self.assertTrue(self.kick("10:50"))
        self.assertEqual(self.kick("12:00"), "")          # twice a day at most
        self.assertEqual(self.calls, 2)
        self.assertEqual(daytrade.kick_daily_run(ny("10:00"), None, self.path, lambda: None), "")

    def test_not_when_the_stock_side_already_ran(self):
        Path(config.STATE_PATH).write_text(json.dumps({"days": [{"date": DAY}]}))
        self.assertEqual(self.kick("10:00"), "")
        self.assertEqual(self.calls, 0)


class BacktestTest(unittest.TestCase):
    def test_prefix_tables_match_live_figures(self):
        rows = [{"t": f"2026-10-{i + 1:02d}T05:00:00Z", "o": 10, "h": 10 + (i % 3), "l": 9 - (i % 2),
                 "c": 10 + (i % 4) * 0.5, "v": 1000 + 10 * i} for i in range(25)]
        tables = dt_backtest.daily_tables(rows)
        st = dt_backtest.stats_before("X", tables, "2026-10-21", "2026-10-20")
        live = daytrade.daily_stats("X", rows[:20])
        self.assertAlmostEqual(st.atr, live.atr)
        self.assertAlmostEqual(st.avg_volume, live.avg_volume)
        self.assertIsNone(dt_backtest.stats_before("X", tables, "2026-10-21", "2026-10-19"))  # no bar yesterday
        self.assertIsNone(dt_backtest.stats_before("X", tables, "2026-10-10", "2026-10-09"))  # too short

    def _minutes(self, symbol, prices, start="09:35"):
        t0 = ny(start)
        return [{"t": daytrade.iso_utc(t0 + timedelta(minutes=i)), "o": o, "h": h, "l": low, "c": c}
                for i, (o, h, low, c) in enumerate(prices)]

    def test_day_trades(self):
        s = Session(DAY, ny("09:30"), ny("16:00"))
        win = Candidate("WIN", 1, 5.0, 50, 51, 49.8, 50.8, 1, 2.0)
        lose = Candidate("LOSE", 2, 4.0, 30, 30.5, 29.9, 30.4, 1, 1.0)
        jump = Candidate("JUMP", 3, 3.0, 20, 20.5, 19.9, 20.4, 1, 1.0)
        minutes = {
            "WIN": self._minutes("WIN", [(50.9, 50.95, 50.8, 50.9), (50.95, 51.2, 50.95, 51.15)]
                                 + [(51.5, 51.6, 51.4, 51.5)] * 400),
            "LOSE": self._minutes("LOSE", [(30.4, 30.6, 30.4, 30.55), (30.55, 30.56, 30.30, 30.35)] + [(30.3, 30.3, 30.3, 30.3)] * 50),
            "JUMP": self._minutes("JUMP", [(20.8, 20.9, 20.7, 20.8)] * 10),
        }
        trades, skipped = dt_backtest.day_trades([win, lose, jump], minutes, s)
        self.assertEqual(skipped, 1)
        by = {t["symbol"]: t for t in trades}
        self.assertEqual(by["LOSE"]["entry"], 30.51)
        self.assertEqual(by["LOSE"]["reason"], "stop-loss")
        self.assertEqual(by["LOSE"]["exit"], daytrade.stop_for(30.51, 1.0))
        self.assertEqual(by["WIN"]["entry"], 51.01)
        self.assertEqual(by["WIN"]["reason"], "sold before the close")
        self.assertEqual(by["WIN"]["exit"], 51.5)
        self.assertEqual([t["symbol"] for t in trades], ["LOSE", "WIN"])  # in the order they broke out

    def test_gap_through_the_stop_sells_at_the_open(self):
        s = Session(DAY, ny("09:30"), ny("16:00"))
        c = Candidate("G", 1, 5.0, 50, 51, 49.8, 50.8, 1, 2.0)
        minutes = {"G": self._minutes("G", [(51.0, 51.1, 51.0, 51.05), (50.5, 50.6, 50.4, 50.5)])}
        (t,), _ = dt_backtest.day_trades([c], minutes, s)
        self.assertEqual(t["exit"], 50.5)
        self.assertLess(t["r"], -1)

    def test_accounts_charge_both_fee_models(self):
        acc = dt_backtest.Accounts()
        acc.apply([{"ret": 0.01}])
        slot = config.DT_REAL_CAPITAL_USD / config.DT_MAX_POSITIONS
        self.assertAlmostEqual(acc.value["before"], 1000 + slot * 0.01)
        self.assertAlmostEqual(acc.value["after_a"], 1000 + slot * 0.01 - (2 + 0.001 * (slot + slot * 1.01)))
        self.assertAlmostEqual(acc.value["after_b"], 1000 + slot * 0.01 - 0.0005 * (slot + slot * 1.01))


class BacktestBroker(DTBroker):
    """Every past day looks like the standard day: same first bars, same price path."""

    def bars(self, symbols, timeframe, start, end, feed, adjustment="raw", pause=0.35):
        if timeframe != "5Min":
            return super().bars(symbols, timeframe, start, end, feed, adjustment, pause)
        out = {}
        for s in symbols:
            sp = self.specs.get(s)
            if sp and sp.first_bar:
                o, h, low, c, v = sp.first_bar
                out[s] = [{"t": start, "o": o, "h": h, "l": low, "c": c, "v": v}]
        return out


class BacktestRunTest(unittest.TestCase):
    def test_end_to_end_on_fake_history(self):
        clock = FakeClock(ny("09:00"))
        result = dt_backtest.run(BacktestBroker(clock), "2026-10-19", "2026-10-30", log=lambda m: None)
        self.assertEqual(result["sessions"], 10)
        self.assertEqual(result["trades"], 10)  # AAA every day; FFF never breaks out
        self.assertEqual(result["skipped_chases"], 20)  # EEE and CCC jump past their high every day
        self.assertEqual({t["symbol"] for t in result["recent_trades"]}, {"AAA"})
        t = result["recent_trades"][0]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (51.10, 52.0, "sold before the close"))  # the price gaps to 51.10 at 9:40: filled at that open
        slot = 1000 / config.DT_MAX_POSITIONS
        ret = 52.0 / 51.10 - 1
        before = 1000.0
        for _ in range(10):
            before += before / config.DT_MAX_POSITIONS * ret
        self.assertAlmostEqual(result["curve"][-1]["before"], round(before, 2), places=2)
        self.assertLess(result["curve"][-1]["after_a"], result["curve"][-1]["after_b"])
        self.assertEqual(result["curve"][-1]["spy_a"], 998.0)
        self.assertGreater(slot, 0)
        json.dumps(result)


class SafetyTest(unittest.TestCase):
    def test_no_live_trading_address(self):
        for name in ("alpaca.py", "daytrade.py", "dt_backtest.py"):
            self.assertNotIn("https://api.alpaca.markets", (Path(daytrade.__file__).parent / name).read_text())


if __name__ == "__main__":
    unittest.main()
