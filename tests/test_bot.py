"""Offline tests: a fake broker and a fake AI, no network, no install needed.

Run with:  python3 -m unittest tests.test_bot
"""
from __future__ import annotations

import json
import math
import os
import statistics
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from bot import accounting, analyst, brain, config, live, research, run, screener, shadows
from bot.alpaca import AlpacaError
from bot.analyst import Card
from bot.brain import Briefing, Decision
from bot.portfolio import PlannedOrder, choose_stop, clean_weights, fit_buys_to_cash, plan_orders


def weekdays_before(day: str, n: int) -> list[str]:
    d, out = date.fromisoformat(day), []
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out[::-1]


def series(day: str, start: float, end: float, volume: float, n: int = 260) -> list[tuple[str, float, float]]:
    """Smooth exponential path from start to end over n trading days before `day`."""
    dates = weekdays_before(day, n)
    k = math.log(end / start) / (n - 1)
    return [(d, start * math.exp(k * i), volume) for i, d in enumerate(dates)]


ASSETS = [
    {"symbol": "AAA", "name": "Alpha Rockets Inc", "exchange": "NASDAQ", "status": "active", "tradable": True},
    {"symbol": "BBB", "name": "Beta Decline Corp", "exchange": "NYSE", "status": "active", "tradable": True},
    {"symbol": "CCC", "name": "Tiny Thin Co", "exchange": "NYSE", "status": "active", "tradable": True},
    {"symbol": "DDD", "name": "Delta Steady Inc", "exchange": "NYSE", "status": "active", "tradable": True},
    {"symbol": "LLL", "name": "Direxion Daily Semis Bull 3X Shares", "exchange": "ARCA", "status": "active", "tradable": True},
    {"symbol": "OTCX", "name": "Pink Sheet Co", "exchange": "OTC", "status": "active", "tradable": True},
    {"symbol": "NOTR", "name": "Halted Co", "exchange": "NYSE", "status": "active", "tradable": False},
] + [{"symbol": s, "name": d, "exchange": "ARCA", "status": "active", "tradable": True}
     for s, d in config.SHADOW_ETFS.items()]


class FakeBroker:
    def __init__(self, cash=100_000.0, day="2026-11-16", is_open=True):
        self.cash_ = cash
        self.day = day
        self.is_open = is_open
        self.prices = {s: 100.0 for s in config.SHADOW_ETFS}
        self.prices.update({"AAA": 150.0, "BBB": 50.0, "CCC": 30.0, "DDD": 100.0, "LLL": 80.0})
        self.shares: dict[str, float] = {}
        self.entry: dict[str, float] = {}
        self.orders: list[tuple] = []
        self.stops: list[tuple] = []      # open stop orders (symbol, qty, price)
        self.filled: list[dict] = []      # closed orders returned by filled_orders
        self.cancels = 0
        self.refuse_sip = False
        self.feeds: list[str] = []
        self.news_calls: list[list] = []

    def clock(self):
        return {"is_open": self.is_open, "timestamp": f"{self.day}T11:30:00-05:00"}

    def assets(self):
        return ASSETS

    def bars_for(self, symbol):
        if symbol == "AAA":
            return series(self.day, 50, 148, 1_000_000)
        if symbol == "BBB":
            return series(self.day, 120, 50, 1_000_000)
        if symbol == "CCC":
            return series(self.day, 10, 30, 1_000)           # strong but illiquid
        if symbol == "DDD":
            return series(self.day, 90, 99, 1_000_000)
        if symbol == "LLL":
            return series(self.day, 20, 79, 5_000_000)       # leveraged: never screened
        rows = series(self.day, 80, 99, 5_000_000)
        return rows + [(self.day, 999.0, 1.0)]               # today's partial bar must be ignored

    def daily_bars(self, symbols, start, end, feed):
        self.feeds.append(feed)
        if feed == "sip" and self.refuse_sip:
            raise AlpacaError("GET /v2/stocks/bars failed with 403: subscription does not permit querying SIP")
        return {s: self.bars_for(s) for s in symbols if s in self.prices}

    def positions_detail(self):
        return [{"symbol": s, "qty": str(q), "market_value": str(q * self.prices[s]),
                 "avg_entry_price": str(self.entry.get(s, self.prices[s])), "current_price": str(self.prices[s]),
                 "unrealized_pl": str(q * (self.prices[s] - self.entry.get(s, self.prices[s]))),
                 "unrealized_plpc": "0", "unrealized_intraday_pl": "0", "change_today": "0"}
                for s, q in self.shares.items() if q]

    def equity(self):
        return self.cash_ + sum(q * self.prices[s] for s, q in self.shares.items())

    def cash(self):
        return self.cash_

    def latest_prices(self, symbols):
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def latest_price(self, symbol):
        return self.prices[symbol]

    def filled_orders(self, after_iso):
        return self.filled

    def news(self, symbols, limit):
        self.news_calls.append(list(symbols))
        return [{"headline": f"{symbols[0]} beats estimates", "summary": "Revenue up 40%.",
                 "created_at": "2026-11-10T13:00:00Z", "symbols": list(symbols), "url": "https://x.com/a",
                 "source": "benzinga"}]

    def cancel_open_orders(self):
        self.cancels += 1
        self.stops = []

    def _fill(self, symbol, side, qty):
        value = qty * self.prices[symbol]
        if side == "buy":
            assert value <= self.cash_ + 1e-6, "bought with money it does not have"
            assert qty == int(qty), "only whole shares are bought"
            self.cash_ -= value
            old = self.shares.get(symbol, 0)
            self.entry[symbol] = (self.entry.get(symbol, 0) * old + value) / (old + qty)
            self.shares[symbol] = old + qty
        else:
            assert qty <= self.shares.get(symbol, 0) + 1e-9, "sold shares it does not hold"
            self.cash_ += value
            self.shares[symbol] = self.shares.get(symbol, 0) - qty
            if self.shares[symbol] <= 1e-9:
                del self.shares[symbol]
        self.orders.append((symbol, side, qty))
        return {"id": str(len(self.orders)), "status": "filled", "filled_qty": qty,
                "filled_avg_price": self.prices[symbol]}

    def market_order(self, symbol, side, qty):
        return self._fill(symbol, side, qty)

    def close_position(self, symbol):
        return self._fill(symbol, "sell", self.shares.get(symbol, 0))

    def wait_final(self, order_id):
        symbol, side, qty = self.orders[int(order_id) - 1]
        return {"status": "filled", "filled_qty": qty, "filled_avg_price": self.prices[symbol]}

    def stop_order(self, symbol, qty, stop_price):
        assert not any(s == symbol for s, _, _ in self.stops), "two stops on the same shares"
        assert qty <= self.shares.get(symbol, 0), "stop on shares not held"
        self.stops.append((symbol, qty, stop_price))
        return {"id": f"stop-{symbol}-{len(self.stops)}", "status": "accepted"}

    def trigger_stop(self, symbol, price):
        """Simulates Alpaca filling a stop-loss between two runs."""
        qty = self.shares[symbol]
        self.prices[symbol] = price
        self._fill(symbol, "sell", qty)
        self.stops = [s for s in self.stops if s[0] != symbol]
        self.filled.append({"id": f"fill-{symbol}", "symbol": symbol, "side": "sell", "type": "stop",
                            "filled_qty": str(qty), "filled_avg_price": str(price),
                            "filled_at": f"{self.day}T15:00:00Z", "submitted_at": f"{self.day}T15:00:00Z"})


class FakeAI:
    """research() returns a fixed briefing, analyse() a fixed card; decide() returns the given answers in turn
    (the last repeats)."""

    def __init__(self, answers, research_cost=0.05, searches=2, card_cost=0.002):
        self.answers = answers
        self.research_cost = research_cost
        self.searches = searches
        self.card_cost = card_cost
        self.calls: list[tuple] = []
        self.research_calls: list[tuple] = []
        self.card_calls: list[str] = []

    def research(self, today, holdings, max_searches=config.NEWS_SEARCH_MAX_USES, candidates=""):
        self.research_calls.append((today, holdings, candidates))
        self.max_searches = max_searches
        return Briefing("ok", "Fed held rates on 2026-11-05.", self.research_cost, self.searches)

    def analyse(self, prompt):
        self.card_calls.append(prompt)
        return Card("ok", analyst.clean_card({"business": "Makes rockets.", "growth_facts": ["Revenue up 40%"],
                                              "catalysts": ["New contract"], "risks": [], "next_earnings_date": "",
                                              "setup_quality": 8, "summary": "Leader."}), self.card_cost)

    def decide(self, system, prompt, allowed):
        self.calls.append((system, prompt, allowed))
        a = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if a.status == "ok":  # keep the symbol check of the real parser
            a = Decision(a.status, {s: w for s, w in a.weights.items() if s in allowed}, a.rationale, a.cost_usd,
                         stops={s: v for s, v in a.stops.items() if s in allowed}, reasons=a.reasons,
                         drivers=a.drivers)
        return a


def ai_says(weights, stops=None, cost=0.10, status="ok", research_cost=0.05):
    answer = Decision(status, weights if status == "ok" else None, "test reason", cost, stops=stops or {},
                      reasons={s: "why" for s in (weights or {})})
    return FakeAI([answer], research_cost=research_cost)


def day_cost(ai: FakeAI, cards: int, samples=config.ENSEMBLE_SIZE, cost=0.10):
    return ai.research_cost + samples * cost + cards * ai.card_cost


class TempFilesMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Never let a test write into the real project files (CI commits them).
        config.REPORT_PATH = os.path.join(self.tmp.name, "report.md")
        config.DASHBOARD_DATA_PATH = os.path.join(self.tmp.name, "data", "dashboard.json")

    def tearDown(self):
        self.tmp.cleanup()


class ScreenTest(unittest.TestCase):
    def setUp(self):
        self.broker = FakeBroker()
        self.names = screener.tradable_assets(ASSETS)
        self.bars = {s: [b for b in self.broker.bars_for(s) if b[0] < self.broker.day] for s in self.names}
        self.result = screener.screen(self.bars, self.names)

    def test_only_main_exchanges_and_tradable(self):
        self.assertIn("AAA", self.names)
        self.assertNotIn("OTCX", self.names)
        self.assertNotIn("NOTR", self.names)

    def test_leader_passes_laggard_illiquid_and_leveraged_do_not(self):
        picked = [c.symbol for c in self.result.candidates]
        self.assertEqual(picked[0], "AAA")
        self.assertNotIn("BBB", picked)     # downtrend
        self.assertNotIn("CCC", picked)     # too little trading
        self.assertNotIn("LLL", picked)     # leveraged product
        self.assertNotIn("CCC", self.result.stats)
        aaa = self.result.stats["AAA"]
        self.assertEqual(self.result.stats["LLL"].rs_percentile, 100.0)   # strongest, but leveraged
        self.assertGreaterEqual(aaa.rs_percentile, config.RS_MIN_PERCENTILE)
        self.assertAlmostEqual(aaa.price, 148.0, places=6)

    def test_breadth_and_regime(self):
        self.assertIsNotNone(self.result.breadth)
        text = screener.regime_text(self.bars, self.result)
        self.assertIn("SPY", text)
        self.assertIn("pass the trend template", text)

    def test_trend_rule_ignores_todays_bar(self):
        self.assertIn("SPY", screener.trend_up(self.bars))
        self.assertNotIn("999.00", screener.describe(self.result.stats["SPY"], "x"))

    def test_too_short_history_is_skipped(self):
        self.assertIsNone(screener.stats_for("NEW", series("2026-11-16", 10, 20, 1e6, n=100)))


class WeightsAndOrdersTest(unittest.TestCase):
    def test_bad_values_dropped_no_cap_total_at_most_one(self):
        w = clean_weights({"AAA": 0.9, "BBB": -0.2, "CCC": "x", "DDD": float("nan")})
        self.assertEqual(w, {"AAA": 0.9})
        w = clean_weights({"AAA": 1.0, "BBB": 1.0})
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_whole_shares_sells_first_unwanted_closed(self):
        held = {"AAA": {"qty": 100, "value": 15_000.0}, "OLD": {"qty": 3.5, "value": 350.0}}
        orders = plan_orders({"AAA": 0.05, "DDD": 0.30}, held, 100_000.0, {"AAA": 150.0, "DDD": 100.0})
        self.assertEqual([(o.symbol, o.side, o.qty, o.close_all) for o in orders],
                         [("OLD", "sell", 0, True), ("AAA", "sell", 66, False), ("DDD", "buy", 300, False)])

    def test_small_change_skipped_and_full_exit_closes(self):
        held = {"AAA": {"qty": 100, "value": 15_000.0}}
        self.assertEqual(plan_orders({"AAA": 0.155}, held, 100_000.0, {"AAA": 150.0}), [])
        held = {"AAA": {"qty": 100.5, "value": 15_075.0}}
        orders = plan_orders({"AAA": 0.000001}, held, 100_000.0, {"AAA": 150.0})
        self.assertTrue(orders[0].close_all)

    def test_buys_never_exceed_cash(self):
        buys = [PlannedOrder("AAA", "buy", 10), PlannedOrder("DDD", "buy", 10)]
        fitted = fit_buys_to_cash(buys, 1_000.0, {"AAA": 150.0, "DDD": 100.0})
        self.assertLessEqual(sum(o.qty * {"AAA": 150.0, "DDD": 100.0}[o.symbol] for o in fitted), 1_000.0)
        self.assertTrue(all(isinstance(o.qty, int) for o in fitted))

    def test_stop_rules(self):
        self.assertEqual(choose_stop(None, 100.0, None), 92.0)            # default 8%
        self.assertEqual(choose_stop(101.0, 100.0, None), 92.0)           # above the price: unusable
        self.assertEqual(choose_stop(95.0, 100.0, None), 95.0)
        self.assertEqual(choose_stop(10.0, 100.0, None), 50.0)            # at most 50% below
        self.assertEqual(choose_stop(90.0, 100.0, 94.0), 94.0)            # never lowered
        self.assertEqual(choose_stop(97.0, 100.0, 94.0), 97.0)            # raised


class EnsembleTest(unittest.TestCase):
    def test_majority_and_median(self):
        a = Decision("ok", {"AAA": 0.5, "DDD": 0.3}, "a", 0.1, stops={"AAA": 140.0})
        b = Decision("ok", {"AAA": 0.4, "BBB": 0.2}, "b", 0.1, stops={"AAA": 136.0})
        c = Decision("ok", {"AAA": 0.3, "CCC": 0.5}, "c", 0.1)
        d = brain.combine([a, b, c])
        self.assertEqual(d.weights, {"AAA": 0.4})          # each other stock was picked only once
        self.assertEqual(d.stops, {"AAA": 138.0})
        self.assertEqual(d.rationale, "b")
        self.assertAlmostEqual(d.cost_usd, 0.3)

    def test_two_valid_answers_must_agree(self):
        a = Decision("ok", {"AAA": 0.5, "DDD": 0.3}, "a", 0.1)
        b = Decision("ok", {"AAA": 0.3}, "b", 0.1)
        bad = Decision("api_error", None, "", 0.0, "timeout")
        d = brain.combine([a, b, bad])
        self.assertEqual(set(d.weights), {"AAA"})
        self.assertAlmostEqual(d.weights["AAA"], 0.4)
        self.assertEqual(brain.combine([a, bad, bad]).status, "no_consensus")
        self.assertEqual(brain.combine([]).status, "no_consensus")

    def test_all_cash_is_a_valid_answer(self):
        d = brain.combine([Decision("ok", {}, "cash", 0.1)] * 3)
        self.assertEqual((d.status, d.weights), ("ok", {}))

    def test_parse_positions(self):
        raw = [{"symbol": "aaa", "weight": 0.2, "stop_price": 140, "reason": "leader"},
               {"symbol": "AAA", "weight": 0.1, "stop_price": "x", "reason": ""},
               {"symbol": "ZZZZ", "weight": 0.5, "stop_price": 1, "reason": "made up"},
               {"symbol": "DDD", "weight": -1, "stop_price": 1, "reason": ""}, "junk"]
        weights, stops, reasons = brain.parse_positions(raw, {"AAA", "DDD"})
        self.assertAlmostEqual(weights["AAA"], 0.3)
        self.assertEqual(set(weights), {"AAA"})
        self.assertEqual(stops, {"AAA": 140.0})

    def test_schema(self):
        schema = brain.output_schema()
        self.assertEqual(set(schema["required"]), {"positions", "drivers", "rationale"})
        self.assertIn("stop_price", schema["properties"]["positions"]["items"]["required"])


class CostTest(unittest.TestCase):
    def test_api_cost_from_usage(self):
        usage = {"input_tokens": 10_000, "output_tokens": 3_000,
                 "cache_creation_input_tokens": None, "cache_read_input_tokens": 0}
        pin, pout = config.MODEL_PRICES_PER_MTOK[config.CLAUDE_MODEL]
        self.assertAlmostEqual(brain.cost_of_usage(usage), (10_000 * pin + 3_000 * pout) / 1e6)
        hin, hout = config.MODEL_PRICES_PER_MTOK[config.ANALYST_MODEL]
        self.assertAlmostEqual(brain.cost_of_usage(usage, config.ANALYST_MODEL), (10_000 * hin + 3_000 * hout) / 1e6)

    def test_search_fees_are_counted(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "server_tool_use": {"web_search_requests": 4}}
        self.assertAlmostEqual(brain.cost_of_usage(usage), 4 * config.WEB_SEARCH_USD)

    def test_worst_case_covers_full_output(self):
        _, pout = config.MODEL_PRICES_PER_MTOK[config.CLAUDE_MODEL]
        self.assertGreater(brain.worst_case_cost("s", "p"), config.MAX_OUTPUT_TOKENS * pout / 1e6)

    def test_fee_is_restated_at_real_size(self):
        expected = config.FEE_PER_ORDER_USD + config.FEE_RATE * 100
        self.assertAlmostEqual(accounting.fee_real(10_000, 100_000), expected)
        self.assertEqual(accounting.fee_real(0, 100_000), 0.0)

    def test_net_result_subtracts_every_cost(self):
        state = {"start_equity": 100_000, "bench_start_price": 100.0, "start_date": "2026-01-01",
                 "api_cost_usd": 5.0, "fees_real_usd": 3.0, "days": []}
        s = accounting.summarize(state, 110_000, 105.0, "2026-01-31")
        real = config.REAL_CAPITAL_USD
        self.assertAlmostEqual(s["strat_net_usd"], real * 0.10 - 3.0 - 5.0 - s["other_costs"])
        self.assertAlmostEqual(s["bench_net_usd"], real * 0.05 - s["bench_fees"])

    def test_drawdown(self):
        self.assertAlmostEqual(accounting.max_drawdown([100, 120, 90, 130]), 0.25)


class AnalystTest(unittest.TestCase):
    def test_card_is_cleaned(self):
        c = analyst.clean_card({"business": "x" * 999, "growth_facts": ["a", "", 3] + ["b"] * 9,
                                "setup_quality": 42, "next_earnings_date": "2026-12-01 (estimated)"})
        self.assertEqual(len(c["business"]), analyst.MAX_CARD_TEXT)
        self.assertEqual(c["growth_facts"][:2], ["a", "3"])
        self.assertEqual(len(c["growth_facts"]), 5)
        self.assertEqual(c["setup_quality"], 10)
        self.assertEqual(c["next_earnings_date"], "2026-12-01")
        self.assertEqual(c["risks"], [])

    def test_freshness(self):
        entry = {"date": "2026-11-10", "card": {"x": 1}}
        self.assertTrue(analyst.is_fresh(entry, "2026-11-16"))
        self.assertFalse(analyst.is_fresh(entry, "2026-11-17"))
        self.assertFalse(analyst.is_fresh(None, "2026-11-16"))


class DailyRunTest(TempFilesMixin, unittest.TestCase):
    def test_first_day_screens_reads_buys_and_sets_stops(self):
        broker = FakeBroker()
        ai = ai_says({"AAA": 0.3}, stops={"AAA": 138.0})
        state, msg = run.run_once(broker, ai, None)
        day = state["days"][0]
        self.assertEqual(day["decision_status"], "ok", msg)
        self.assertEqual(day["screen"]["candidates"][0]["symbol"], "AAA")
        self.assertEqual(len(ai.card_calls), len(day["screen"]["candidates"]))
        self.assertIn("beats estimates", ai.card_calls[0])
        self.assertEqual(ai.max_searches, config.NEWS_SEARCH_MAX_USES)
        self.assertIn("AAA", ai.research_calls[0][2])
        self.assertAlmostEqual(state["api_cost_usd"], day_cost(ai, len(ai.card_calls)))
        self.assertEqual(broker.shares["AAA"], 200)          # 30% of $100,000 at $150, whole shares
        self.assertEqual(broker.stops, [("AAA", 200, 138.0)])
        self.assertEqual(state["stops"], {"AAA": 138.0})
        self.assertGreater(state["fees_real_usd"], 0)
        self.assertEqual(day["feed"], "sip")
        prompt = ai.calls[0][1]
        self.assertIn("Alpha Rockets", prompt)
        self.assertIn("setup 8/10", prompt)
        self.assertIn("Fed held rates", prompt)
        self.assertNotIn("999.00", prompt)                    # today's partial bar never shown
        self.assertEqual(len({c[1] for c in ai.calls}), 1)    # all samples see the same prompt
        run.write_report(state, broker.equity(), 100.0, day["date"])
        self.assertIn("Stop-losses in place: AAA at 138.00", Path(config.REPORT_PATH).read_text())

    def test_any_tradable_stock_can_be_bought_unknown_ones_cannot(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"DDD": 0.2, "ZZZZ": 0.2}), None)
        self.assertEqual(broker.shares, {"DDD": 200})
        self.assertEqual(state["stops"], {"DDD": 92.0})       # no stop given: 8% below

    def test_old_etf_positions_are_sold_if_not_wanted(self):
        broker = FakeBroker()
        broker.shares = {"SPY": 499.5}
        broker.cash_ -= 49_950.0
        state, _ = run.run_once(broker, ai_says({"AAA": 0.5}), None)
        self.assertNotIn("SPY", broker.shares)
        self.assertEqual(state["days"][0]["weights_before"], {"SPY": 0.4995})

    def test_second_day_reuses_cards_and_never_lowers_a_stop(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}, stops={"AAA": 140.0}), None)
        broker.day = "2026-11-17"
        ai = ai_says({"AAA": 0.3}, stops={"AAA": 120.0})
        state, _ = run.run_once(broker, ai, state)
        self.assertEqual(ai.card_calls, [])
        self.assertEqual(ai.max_searches, config.ROUTINE_SEARCH_MAX_USES)
        self.assertEqual(state["days"][-1]["trigger"], "daily review")
        self.assertEqual(broker.stops, [("AAA", 200, 140.0)])
        self.assertEqual(broker.orders, [("AAA", "buy", 200)])  # nothing to trade
        self.assertIn("2026-11-16 to 2026-11-17", ai.calls[0][1])  # scorecard

    def test_stop_fill_is_recorded_with_its_fee(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}, stops={"AAA": 138.0}), None)
        broker.trigger_stop("AAA", 138.0)
        broker.day = "2026-11-17"
        fees_before = state["fees_real_usd"]
        state, _ = run.run_once(broker, ai_says({}), state)
        fills = state["days"][-1]["stop_fills"]
        self.assertEqual([f["symbol"] for f in fills], ["AAA"])
        self.assertAlmostEqual(state["fees_real_usd"] - fees_before, fills[0]["fee_real_usd"], places=3)
        self.assertEqual(state["stops"], {})
        broker.day = "2026-11-18"
        state, _ = run.run_once(broker, ai_says({}), state)
        self.assertNotIn("stop_fills", state["days"][-1])     # counted once only

    def test_price_through_the_stop_sells_now(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}, stops={"AAA": 145.0}), None)
        broker.prices["AAA"] = 144.0          # fell through the stop while no order was live
        broker.day = "2026-11-17"
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}, stops={"AAA": 140.0}), state)
        self.assertNotIn("AAA", broker.shares)
        self.assertEqual(broker.stops, [])

    def test_failed_answers_hold_and_keep_stops(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}, stops={"AAA": 138.0}), None)
        broker.day = "2026-11-17"
        ai = ai_says(None, cost=0.05, status="refused")
        state, _ = run.run_once(broker, ai, state)
        self.assertEqual(len(ai.calls), 2)    # a majority is impossible after two refusals
        self.assertEqual(state["days"][-1]["decision_status"], "no_consensus")
        self.assertEqual(broker.cancels, 1)   # only the first day's
        self.assertEqual(broker.stops, [("AAA", 200, 138.0)])

    def test_budget_stops_ai_calls_but_not_stops(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}), None)
        state["api_cost_usd"] = config.API_BUDGET_USD - config.RESEARCH_WORST_CASE_USD
        broker.day = "2026-11-17"
        ai = ai_says({})
        state, _ = run.run_once(broker, ai, state)
        self.assertEqual((ai.calls, ai.research_calls, ai.card_calls), ([], [], []))
        self.assertEqual(state["days"][-1]["decision_status"], "budget_reached")
        self.assertEqual(len(broker.stops), 1)

    def test_cost_saved_before_trading(self):
        saved = []
        broker = FakeBroker()
        ai = ai_says({"AAA": 0.3})
        run.run_once(broker, ai, None, save=lambda s: saved.append((round(s["api_cost_usd"], 6), len(broker.orders))))
        self.assertAlmostEqual(saved[-1][0], day_cost(ai, len(ai.card_calls)))
        self.assertTrue(all(n == 0 for _, n in saved))

    def test_sip_refused_falls_back_to_iex(self):
        broker = FakeBroker()
        broker.refuse_sip = True
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}), None)
        self.assertEqual(state["days"][0]["feed"], "iex")
        self.assertEqual(state["days"][0]["decision_status"], "ok")

    def test_second_run_same_day_and_closed_market_do_nothing(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3}), None)
        ai = ai_says({})
        state, msg = run.run_once(broker, ai, state)
        self.assertIn("Already ran", msg)
        self.assertIsNone(run.run_once(FakeBroker(is_open=False), ai, None)[0])
        self.assertEqual(ai.calls, [])

    def test_dashboard_numbers_match_the_report(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3, "DDD": 0.2}), None)
        broker.prices["SPY"], broker.prices["AAA"] = 110.0, 160.0
        broker.day = "2026-11-17"
        state, _ = run.run_once(broker, ai_says({"AAA": 0.3, "DDD": 0.2}), state)
        run.write_report(state, broker.equity(), broker.prices["SPY"], "2026-11-17")
        data = json.loads(Path(config.DASHBOARD_DATA_PATH).read_text())
        last = data["series"][-1]
        self.assertEqual([p["date"] for p in data["series"]], ["2026-11-16", "2026-11-17"])
        self.assertAlmostEqual(last["ai_after_costs"], config.REAL_CAPITAL_USD + data["summary"]["strat_net_usd"], places=1)
        self.assertAlmostEqual(last["spy_after_costs"], config.REAL_CAPITAL_USD + data["summary"]["bench_net_usd"], places=1)
        self.assertAlmostEqual(last["api_cum"], state["api_cost_usd"], places=4)
        self.assertAlmostEqual(last["fees_cum"], state["fees_real_usd"], places=3)
        self.assertAlmostEqual(sum(data["api_cost_split"].values()), state["api_cost_usd"], places=4)
        for name in shadows.SHADOWS:
            self.assertIsNotNone(last[name])
        self.assertEqual(data["candidates"][0]["symbol"], "AAA")
        self.assertEqual(data["candidates"][0]["card"]["setup_quality"], 8)
        self.assertTrue(data["candidates"][0]["held"])
        self.assertEqual(data["stops"]["AAA"], 147.2)         # 8% below the second day's price, raised
        self.assertEqual(data["names"]["AAA"], "Alpha Rockets Inc")


class ShadowTest(unittest.TestCase):
    def _day(self, date_, spy=100.0, trend=()):
        prices = {s: 100.0 for s in config.SHADOW_ETFS}
        prices["SPY"] = spy
        return {"date": date_, "prices": prices, "trend_up": list(trend)}

    def test_60_40_by_hand(self):
        days = [self._day("2026-11-16"), self._day("2026-11-17", spy=110.0)]
        values = shadows.simulate(days, "s_6040")
        # Buy IEF 400 (fee 1.40), then SPY 597.00 (fee 1.597) with the cash left: 0.003 cash remains.
        self.assertAlmostEqual(values[0]["value"], 997.0, places=2)
        self.assertAlmostEqual(values[1]["value"], 0.003 + 597.0 * 1.1 + 400.0, places=2)

    def test_trend_rule_holds_cash_when_nothing_trends(self):
        values = shadows.simulate([self._day("2026-11-16"), self._day("2026-11-17", spy=50.0)], "s_trend")
        self.assertEqual([v["value"] for v in values], [1000.0, 1000.0])

    def test_qqq_is_bought_once_and_held(self):
        days = [self._day("2026-11-16"), self._day("2026-12-01")]
        days[1]["prices"]["QQQ"] = 120.0
        values = shadows.simulate(days, "s_qqq")
        bought = 1000.0 - (config.FEE_PER_ORDER_USD + config.FEE_RATE * 1000.0)  # cash minus the fee estimate
        fee = config.FEE_PER_ORDER_USD + config.FEE_RATE * bought
        self.assertAlmostEqual(values[1]["value"], 1000.0 - bought - fee + bought * 1.2, places=2)

    def test_equal_weight_rebalances_only_in_a_new_month(self):
        days = [self._day("2026-11-16"), self._day("2026-11-30", spy=200.0), self._day("2026-12-01", spy=200.0)]
        values = shadows.simulate(days, "s_equal")
        self.assertGreater(values[1]["value"] - values[2]["value"], 0)  # December rebalance costs fees


class EconomyTest(unittest.TestCase):
    def test_missing_values_skipped_and_inflation_yearly(self):
        rows = research.parse_fred_csv("observation_date,CPIAUCSL\n2025-09-01,300\n2025-10-01,.\n2026-09-01,309\n")
        self.assertEqual(rows, [("2025-09-01", 300.0), ("2026-09-01", 309.0)])
        self.assertIn("3.0%", research.describe("CPIAUCSL", "", rows, "2026-11-13"))

    def test_failing_series_is_left_out(self):
        def fetch(url):
            if "VIXCLS" in url:
                raise OSError("down")
            return "observation_date,X\n2026-08-01,4.00\n2026-10-14,4.10\n2026-11-12,4.25\n"
        text = research.economy_snapshot("2026-11-13", fetch)
        self.assertNotIn("VIX", text)
        self.assertIn("US 10-year Treasury yield (%): 4.25 on 2026-11-12 (change: 1 month +0.15, 3 months +0.25)", text)


class LiveBroker(FakeBroker):
    """Adds the read-only views the live dashboard uses."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.filled = []
        self.fail_news = False
        self.last_equity = None

    def account(self):
        eq = self.equity()
        return {"equity": str(eq), "last_equity": str(self.last_equity or eq), "cash": str(self.cash_)}

    def positions_detail(self):
        return [{"symbol": s, "qty": str(q), "avg_entry_price": "100", "current_price": str(self.prices[s]),
                 "market_value": str(q * self.prices[s]), "unrealized_pl": str(q * (self.prices[s] - 100)),
                 "unrealized_plpc": str(self.prices[s] / 100 - 1), "unrealized_intraday_pl": "0", "change_today": "0"}
                for s, q in self.shares.items() if q]

    def filled_orders(self, after_iso):
        return self.filled

    def portfolio_history(self):
        return {"timestamp": [1794580200, 1794581100, 1794582000], "equity": [100000.0, 100500.0, None]}

    def news(self, symbols, limit):
        if self.fail_news:
            raise OSError("down")
        self.news_calls.append(list(symbols))
        return [{"headline": "Fed holds", "url": "javascript:alert(1)", "symbols": ["AAA", "AAPL"],
                 "created_at": "2026-11-16T15:00:00Z", "source": "benzinga", "summary": "x"}]


def order(oid, symbol, side, qty, price, when):
    return {"id": oid, "symbol": symbol, "side": side, "filled_qty": str(qty), "filled_avg_price": str(price),
            "filled_at": when, "submitted_at": when}


class LiveTest(unittest.TestCase):
    def _state(self):
        return {"start_date": "2026-11-16", "start_equity": 100_000.0, "days": [
            {"date": "2026-11-16", "equity": 100_000.0, "rationale": "first",
             "orders": [{"symbol": "SPY", "side": "buy", "fee_real_usd": 1.1, "order_id": "a"}]},
            {"date": "2026-11-17", "equity": 100_000.0, "rationale": "second",
             "orders": [{"symbol": "SPY", "side": "buy", "fee_real_usd": 1.2}]},
            {"date": "2026-11-18", "equity": 100_000.0, "rationale": "third", "orders": []},
        ]}

    def test_realised_pnl_against_average_price(self):
        orders = [
            order("old", "SPY", "buy", 5, 90, "2026-11-13T15:00:00Z"),        # before the test: ignored
            order("a", "SPY", "buy", 100, 100, "2026-11-16T15:00:00Z"),
            order("b", "SPY", "buy", 100, 110, "2026-11-17T15:00:00Z"),
            order("c", "SPY", "sell", 50, 120, "2026-11-18T15:00:00Z"),
            order("d", "GLD", "buy", 0, 0, "2026-11-18T15:00:00Z"),           # nothing filled: ignored
        ]
        rows, stats = live.trades_from_orders(orders, self._state())
        self.assertEqual([r["side"] for r in rows], ["buy", "buy", "sell"])
        self.assertEqual([r["fee_real_usd"] for r in rows[:2]], [1.1, 1.2])   # by order id, then by day
        self.assertAlmostEqual(rows[2]["fee_real_usd"], accounting.fee_real(6000, 100_000), places=4)
        self.assertAlmostEqual(rows[2]["realized_real_usd"], 50 * (120 - 105) * 0.01)  # $1,000 / $100,000
        self.assertAlmostEqual(rows[2]["realized_pct"], 120 / 105 - 1)
        self.assertAlmostEqual(rows[0]["amount_real_usd"], 100.0)
        self.assertEqual(rows[2]["reason"], "third")
        self.assertEqual((stats["trades"], stats["closed_sells"], stats["winning_sells"]), (3, 1, 1))

    def test_build_and_refresh(self):
        broker = LiveBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.5}, stops={"AAA": 140.0}), None)
        start = state["start_equity"]
        broker.prices["AAA"] = 160.0           # bought 333 shares at 150
        broker.last_equity = start
        broker.filled = [order("1", "AAA", "buy", 333, 150, "2026-11-16T15:00:00Z")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            self.assertIn("updated", live.refresh(broker, state, path, "2026-11-16T15:30:00Z"))
            data = json.loads(path.read_text())
            aaa = next(p for p in data["positions"] if p["symbol"] == "AAA")
            scale = config.REAL_CAPITAL_USD / start
            self.assertAlmostEqual(aaa["pl_real_usd"], round(333 * 60 * scale, 2))   # LiveBroker: entry 100
            self.assertEqual(aaa["stop"], 140.0)
            self.assertEqual(aaa["name"], "Alpha Rockets Inc")
            self.assertAlmostEqual(data["account"]["today_real_usd"], round(333 * 10 * scale, 2))
            self.assertEqual(data["headlines"][0]["url"], "")              # unsafe link removed
            self.assertEqual(data["headlines"][0]["symbols"], ["AAA"])
            self.assertEqual(broker.news_calls[-1], ["AAA", "SPY"])
            self.assertEqual(len(data["intraday"]), 2)                     # empty point skipped
            self.assertEqual(live.refresh(broker, state, path, "2026-11-16T15:45:00Z"), "No change.")
            broker.fail_news = True
            broker.prices["AAA"] = 161.0
            self.assertIn("headlines unavailable", live.refresh(broker, state, path, "2026-11-16T16:00:00Z"))
            self.assertIn("positions", json.loads(path.read_text()))
            broker.is_open = False
            live.refresh(broker, state, path, "2026-11-16T21:10:00Z")
            self.assertFalse(json.loads(path.read_text())["market_open"])
            self.assertIn("already saved", live.refresh(broker, state, path, "2026-11-16T21:25:00Z"))

    def test_nothing_before_the_test_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            self.assertIn("not started", live.refresh(LiveBroker(), None, path, "2026-11-16T15:30:00Z"))
            self.assertFalse(path.exists())

    def test_orders_keep_their_id(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"AAA": 0.5}), None)
        self.assertEqual(state["days"][0]["orders"][0]["order_id"], "1")
        self.assertEqual(state["days"][0]["stop_orders"][0]["order_id"], "stop-AAA-1")

    def test_stop_fill_fee_comes_from_the_ledger(self):
        state = self._state()
        state["days"][2]["stop_fills"] = [{"symbol": "SPY", "side": "sell", "fee_real_usd": 1.7, "order_id": "s"}]
        orders = [order("a", "SPY", "buy", 100, 100, "2026-11-16T15:00:00Z"),
                  order("s", "SPY", "sell", 100, 92, "2026-11-18T15:00:00Z")]
        rows, _ = live.trades_from_orders(orders, state)
        self.assertEqual(rows[1]["fee_real_usd"], 1.7)


class MetricsTest(unittest.TestCase):
    def _values(self, returns, start=1000.0):
        out, v = [], start
        for r in returns:
            v *= 1 + r
            out.append(v)
        return out

    def test_sharpe_and_sortino_by_hand(self):
        r = [0.02, -0.01, 0.02, -0.01, 0.02, -0.01]
        m = accounting.risk_metrics(self._values(r), 1000.0, 0.0504)
        rf = 0.0504 / 252
        sd = statistics.stdev(r)
        self.assertAlmostEqual(m["sharpe"], (statistics.mean(r) - rf) / sd * math.sqrt(252))
        downside = math.sqrt(sum(min(0, x - rf) ** 2 for x in r) / len(r))
        self.assertAlmostEqual(m["sortino"], (statistics.mean(r) - rf) / downside * math.sqrt(252))
        self.assertAlmostEqual(m["volatility"], sd * math.sqrt(252))
        self.assertAlmostEqual(m["up_days"], 0.5)
        self.assertAlmostEqual(m["worst_day"], -0.01)
        self.assertIsNone(m["annual_return"])                   # fewer than 20 days

    def test_too_few_days_shows_nothing_misleading(self):
        m = accounting.risk_metrics([1010.0, 1005.0], 1000.0, 0.04)
        self.assertIsNone(m["sharpe"])
        self.assertAlmostEqual(m["total_return"], 0.005)
        self.assertAlmostEqual(m["max_drawdown"], 1 - 1005 / 1010)

    def test_beta_of_double_bet_is_two(self):
        b = [0.01, -0.02, 0.015, 0.005, -0.01, 0.02]
        m = accounting.risk_metrics(self._values([2 * x for x in b]), 1000.0, 0.0, self._values(b))
        self.assertAlmostEqual(m["beta"], 2.0)
        self.assertAlmostEqual(m["correlation"], 1.0)

    def test_dashboard_has_metrics_scorecard_and_drivers(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config.REPORT_PATH = os.path.join(tmp.name, "report.md")
        config.DASHBOARD_DATA_PATH = os.path.join(tmp.name, "data", "dashboard.json")
        drivers = [{"factor": "Fed hike odds up", "input": "news", "symbols": ["TLT"], "effect": "lower"}]
        answer = Decision("ok", {"AAA": 0.5}, "why", 0.1, drivers=drivers)
        broker = FakeBroker()
        state, _ = run.run_once(broker, FakeAI([answer]), None, economy=lambda t: "- VIX: 15")
        broker.day, broker.prices["SPY"] = "2026-11-17", 104.0
        broker.prices["AAA"] = 155.0
        state, _ = run.run_once(broker, FakeAI([answer]), state)
        state["risk_free_rate"] = 0.04
        run.write_report(state, broker.equity(), 104.0, "2026-11-17")
        data = json.loads(Path(config.DASHBOARD_DATA_PATH).read_text())
        self.assertEqual(data["metrics"]["ai_after_costs"]["days"], 2)
        self.assertEqual(data["risk_free_rate"], 0.04)
        first = data["log"][-1]
        self.assertEqual(first["drivers"], drivers)
        self.assertEqual(first["economy"], "- VIX: 15")
        self.assertEqual(first["weights_before"], {})
        self.assertEqual(first["reasons"], {})
        self.assertEqual(first["stops_set"], {"AAA": 138.0})
        row = data["scorecard"][0]
        self.assertAlmostEqual(row["added"], row["ai"] - row["keep"])


class DriversAndSourcesTest(unittest.TestCase):
    def test_drivers_are_cleaned(self):
        raw = [{"factor": " Yields up ", "input": "rumour", "symbols": ["tlt", " ", "AAPL"], "effect": "sell!"},
               {"factor": "", "input": "news", "symbols": [], "effect": "raise"}, "junk"] + \
              [{"factor": f"f{i}", "input": "prices", "symbols": [], "effect": "hold"} for i in range(20)]
        out = brain.clean_drivers(raw)
        self.assertEqual(out[0], {"factor": "Yields up", "input": "other", "symbols": ["TLT", "AAPL"], "effect": "hold"})
        self.assertEqual(len(out), brain.MAX_DRIVERS)
        self.assertEqual(brain.clean_drivers(None), [])

    def test_sources_prefer_citations_and_drop_unsafe_links(self):
        cite = lambda url, title: SimpleNamespace(url=url, title=title)
        text = SimpleNamespace(type="text", text="x", citations=[cite("https://a.com/1", "A"), cite("https://a.com/1", "A"),
                                                                  cite("javascript:alert(1)", "bad")])
        results = SimpleNamespace(type="web_search_tool_result", content=[cite("https://b.com", "B")])
        self.assertEqual(brain.sources_from_content([[text, results]]), [{"url": "https://a.com/1", "title": "A"}])
        plain = SimpleNamespace(type="text", text="x", citations=None)
        self.assertEqual(brain.sources_from_content([[plain, results]]), [{"url": "https://b.com", "title": "B"}])

    def test_schema_requires_drivers(self):
        schema = brain.output_schema()
        self.assertIn("drivers", schema["required"])
        self.assertEqual(schema["properties"]["drivers"]["items"]["properties"]["input"]["enum"], brain.DRIVER_INPUTS)


class SafetyTest(unittest.TestCase):
    def test_no_live_trading_address_in_code(self):
        source = (Path(run.__file__).parent / "alpaca.py").read_text()
        self.assertNotIn("https://api.alpaca.markets", source)
        self.assertIn("https://paper-api.alpaca.markets", source)


if __name__ == "__main__":
    unittest.main()
