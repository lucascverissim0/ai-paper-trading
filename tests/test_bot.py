"""Offline tests: a fake broker and a fake AI, no network, no install needed.

Run with:  python3 -m unittest tests.test_bot
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import math
import statistics
from types import SimpleNamespace

from bot import accounting, brain, config, live, research, run, shadows, triggers
from bot.brain import Briefing, Decision
from bot.portfolio import PlannedOrder, clean_weights, fit_buys_to_cash, plan_orders


class FakeBroker:
    def __init__(self, cash=100_000.0, day="2026-11-16", is_open=True):
        self.cash_ = cash
        self.prices = {s: 100.0 for s in config.UNIVERSE}
        self.shares: dict[str, float] = {}
        self.day = day
        self.is_open = is_open
        self.orders: list[tuple] = []
        self.closed_all = False
        self.closes_override = None

    def clock(self):
        return {"is_open": self.is_open, "timestamp": f"{self.day}T11:30:00-05:00"}

    def positions(self):
        return {s: q * self.prices[s] for s, q in self.shares.items() if q}

    def equity(self):
        return self.cash_ + sum(self.positions().values())

    def cash(self):
        return self.cash_

    def latest_prices(self, symbols):
        return {s: self.prices[s] for s in symbols}

    def latest_price(self, symbol):
        return self.prices[symbol]

    def daily_closes(self, symbols, start_iso):
        if self.closes_override:
            return {s: list(self.closes_override.get(s, self.closes_override["SPY"])) for s in symbols}
        return {s: [(f"2026-{m:02d}-{d:02d}", 100.0 + m + d / 100) for m in range(1, 12) for d in range(1, 21)]
                + [(self.day, 999.0)] for s in symbols}

    def cancel_open_orders(self):
        pass

    def market_order(self, symbol, side, notional):
        qty = notional / self.prices[symbol]
        if side == "buy":
            assert notional <= self.cash_ + 1e-6, "bought with money it does not have"
            self.cash_ -= notional
            self.shares[symbol] = self.shares.get(symbol, 0) + qty
        else:
            self.cash_ += notional
            self.shares[symbol] = self.shares.get(symbol, 0) - qty
        self.orders.append((symbol, side, notional))
        return {"id": str(len(self.orders)), "status": "filled", "filled_qty": qty, "filled_avg_price": self.prices[symbol]}

    def close_position(self, symbol):
        value = self.shares.get(symbol, 0) * self.prices[symbol]
        return self.market_order(symbol, "sell", value)

    def close_all(self):
        for s in list(self.shares):
            self.close_position(s)
        self.closed_all = True

    def wait_final(self, order_id):
        return {"status": "filled", "filled_qty": self.orders[int(order_id) - 1][2] / 100.0, "filled_avg_price": 100.0}


class FakeAI:
    """research() returns a fixed briefing; decide() returns the given answers in turn (the last repeats)."""

    def __init__(self, answers, research_cost=0.05, searches=2):
        self.answers = answers
        self.research_cost = research_cost
        self.searches = searches
        self.calls: list[tuple] = []
        self.research_calls: list[tuple] = []

    def research(self, today, holdings):
        self.research_calls.append((today, holdings))
        return Briefing("ok", "Fed held rates on 2026-11-05.", self.research_cost, self.searches)

    def decide(self, system, prompt):
        self.calls.append((system, prompt))
        return self.answers[min(len(self.calls), len(self.answers)) - 1]


def ai_says(weights, cost=0.10, status="ok", review=1, research_cost=0.05):
    answer = Decision(status, weights if status == "ok" else None, "test reason", cost, next_review_days=review)
    return FakeAI([answer], research_cost=research_cost)


def wake_cost(cost=0.10, research_cost=0.05, samples=config.ENSEMBLE_SIZE):
    return research_cost + samples * cost


class WeightsTest(unittest.TestCase):
    def test_bad_values_become_zero_and_caps_apply(self):
        w = clean_weights({"SPY": 0.9, "QQQ": -0.2, "GLD": "x", "TLT": float("nan"), "XYZ": 0.5})
        self.assertEqual(w["SPY"], config.MAX_WEIGHT)
        self.assertEqual(w["QQQ"], 0.0)
        self.assertEqual(w["GLD"], 0.0)
        self.assertEqual(w["TLT"], 0.0)
        self.assertNotIn("XYZ", w)

    def test_total_never_above_one(self):
        w = clean_weights({s: 0.5 for s in config.UNIVERSE})
        self.assertAlmostEqual(sum(w.values()), 1.0)


class OrdersTest(unittest.TestCase):
    def test_sells_first_small_changes_skipped(self):
        orders = plan_orders({"SPY": 0.3, "QQQ": 0.01, "TLT": 0.0}, {"SPY": 50_000.0, "TLT": 10_000.0}, 100_000.0)
        self.assertEqual([(o.symbol, o.side, o.close_all) for o in orders],
                         [("SPY", "sell", False), ("TLT", "sell", True)])  # QQQ 1% < 2% threshold
        self.assertEqual(orders[0].notional, 20_000.0)

    def test_unknown_holding_is_closed(self):
        orders = plan_orders({}, {"AAPL": 500.0}, 100_000.0)
        self.assertEqual((orders[0].symbol, orders[0].close_all), ("AAPL", True))

    def test_buys_never_exceed_cash(self):
        buys = [PlannedOrder("SPY", "buy", 600.0), PlannedOrder("GLD", "buy", 400.0)]
        fitted = fit_buys_to_cash(buys, 500.0)
        self.assertLessEqual(sum(o.notional for o in fitted), 500.0)


class CostTest(unittest.TestCase):
    def test_api_cost_from_usage(self):
        usage = {"input_tokens": 10_000, "output_tokens": 3_000,
                 "cache_creation_input_tokens": None, "cache_read_input_tokens": 0}
        pin, pout = config.MODEL_PRICES_PER_MTOK[config.CLAUDE_MODEL]
        self.assertAlmostEqual(brain.cost_of_usage(usage), (10_000 * pin + 3_000 * pout) / 1e6)

    def test_worst_case_covers_full_output(self):
        _, pout = config.MODEL_PRICES_PER_MTOK[config.CLAUDE_MODEL]
        self.assertGreater(brain.worst_case_cost("s", "p"), config.MAX_OUTPUT_TOKENS * pout / 1e6)

    def test_fee_is_restated_at_real_size(self):
        # $10,000 traded on a $100,000 paper account = $100 at $1,000 real size.
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


class DailyRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Never let a test write into the real project files (CI commits them).
        config.REPORT_PATH = os.path.join(self.tmp.name, "report.md")
        config.DASHBOARD_DATA_PATH = os.path.join(self.tmp.name, "data", "dashboard.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_day_trades_and_records_costs(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "GLD": 0.2}), None)
        self.assertEqual(state["days"][0]["decision_status"], "ok")
        self.assertAlmostEqual(state["api_cost_usd"], wake_cost())
        self.assertEqual(len(state["days"][0]["samples"]), config.ENSEMBLE_SIZE)
        self.assertGreater(state["fees_real_usd"], 0)
        self.assertAlmostEqual(broker.positions()["SPY"], 50_000, delta=1)
        run.write_report(state, broker.equity(), 100.0, state["days"][-1]["date"])
        self.assertIn("after all costs", Path(config.REPORT_PATH).read_text())

    def test_dashboard_numbers_match_the_report(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "GLD": 0.2}), None)
        broker.prices["SPY"] = 110.0
        broker.day = "2026-11-17"
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "GLD": 0.2}), state)
        run.write_report(state, broker.equity(), broker.prices["SPY"], "2026-11-17")
        data = json.loads(Path(config.DASHBOARD_DATA_PATH).read_text())
        last = data["series"][-1]
        self.assertEqual([p["date"] for p in data["series"]], ["2026-11-16", "2026-11-17"])
        self.assertAlmostEqual(last["ai_after_costs"], config.REAL_CAPITAL_USD + data["summary"]["strat_net_usd"], places=1)
        self.assertAlmostEqual(last["spy_after_costs"], config.REAL_CAPITAL_USD + data["summary"]["bench_net_usd"], places=1)
        self.assertAlmostEqual(last["api_cum"], state["api_cost_usd"], places=4)
        self.assertAlmostEqual(last["fees_cum"], state["fees_real_usd"], places=3)
        self.assertTrue(all(t["amount_real_usd"] <= config.REAL_CAPITAL_USD for t in data["trades"]))

    def test_todays_partial_bar_is_not_shown_to_ai(self):
        broker = FakeBroker()
        ai = ai_says({})
        run.run_once(broker, ai, None)
        self.assertNotIn("999.00", ai.calls[0][1])

    def test_second_run_same_day_does_nothing(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), None)
        ai = ai_says({"SPY": 0.0})
        state, msg = run.run_once(broker, ai, state)
        self.assertEqual(ai.calls, [])
        self.assertIn("Already ran", msg)

    def test_market_closed_does_nothing(self):
        ai = ai_says({"SPY": 0.5})
        state, _ = run.run_once(FakeBroker(is_open=False), ai, None)
        self.assertIsNone(state)
        self.assertEqual(ai.calls, [])

    def test_budget_stops_ai_calls(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), None)
        # Less left than research + three answers could cost at worst: no call at all, not even research.
        state["api_cost_usd"] = config.API_BUDGET_USD - config.RESEARCH_WORST_CASE_USD
        broker.day = "2026-11-17"
        ai = ai_says({"SPY": 0.0})
        state, _ = run.run_once(broker, ai, state)
        self.assertEqual(ai.calls, [])
        self.assertEqual(ai.research_calls, [])
        self.assertEqual(state["days"][-1]["decision_status"], "budget_reached")
        self.assertLessEqual(state["api_cost_usd"], config.API_BUDGET_USD)

    def test_loss_limit_sells_everything_and_stops(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "QQQ": 0.5}), None)
        broker.prices = {s: 60.0 for s in config.UNIVERSE}  # 40% fall on everything held
        broker.day = "2026-11-17"
        ai = ai_says({"SPY": 0.5})
        state, _ = run.run_once(broker, ai, state)
        self.assertTrue(broker.closed_all)
        self.assertTrue(state["halted"])
        self.assertEqual(ai.calls, [])

    def test_failed_ai_answer_holds_but_counts_cost(self):
        broker = FakeBroker()
        ai = ai_says(None, cost=0.05, status="refused")
        state, _ = run.run_once(broker, ai, None)
        self.assertEqual(broker.orders, [])
        # After two refusals a majority is impossible: the third answer is not paid for.
        self.assertEqual(len(ai.calls), 2)
        self.assertAlmostEqual(state["api_cost_usd"], wake_cost(0.05, samples=2))
        self.assertEqual(state["days"][-1]["decision_status"], "no_consensus")

    def test_cost_saved_before_trading(self):
        saved = []
        broker = FakeBroker()
        run.run_once(broker, ai_says({"SPY": 0.5}), None,
                     save=lambda s: saved.append((round(s["api_cost_usd"], 6), len(broker.orders))))
        self.assertAlmostEqual(saved[-1][0], wake_cost())
        self.assertTrue(all(n_orders == 0 for _, n_orders in saved))

    # --- v2: when Claude is woken up -------------------------------------------------
    def _decided(self, review=1, weights=None):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says(weights or {"SPY": 0.5, "GLD": 0.2}, review=review), None)
        return broker, state

    def test_waits_until_its_review_date_at_no_cost(self):
        broker, state = self._decided(review=3)  # Monday 16th -> review Thursday 19th
        cost = state["api_cost_usd"]
        for day, expected in (("2026-11-17", "waiting"), ("2026-11-18", "waiting"), ("2026-11-19", "ok")):
            broker.day = day
            ai = ai_says({"SPY": 0.5, "GLD": 0.2}, review=3)
            state, _ = run.run_once(broker, ai, state)
            self.assertEqual(state["days"][-1]["decision_status"], expected, day)
            self.assertEqual(bool(ai.calls), expected == "ok")
        self.assertAlmostEqual(state["api_cost_usd"], cost + wake_cost())
        self.assertIn("scheduled review", state["days"][-1]["trigger"])
        self.assertIn("SPY", state["days"][1]["prices"])  # waiting days still record prices

    def test_big_spy_day_wakes_it_early(self):
        broker, state = self._decided(review=10)
        broker.day = "2026-11-18"
        broker.closes_override = {"SPY": [("2026-11-13", 100.0), ("2026-11-16", 100.0), ("2026-11-17", 97.0)]}
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), state)
        self.assertIn("SPY moved -3.0%", state["days"][-1]["trigger"])

    def test_held_etf_jump_wakes_it_early(self):
        broker, state = self._decided(review=10)
        broker.day = "2026-11-18"
        broker.closes_override = {
            "SPY": [("2026-11-13", 100.0), ("2026-11-16", 100.5), ("2026-11-17", 101.0)],
            "GLD": [("2026-11-13", 100.0), ("2026-11-16", 103.0), ("2026-11-17", 106.0)],
        }
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), state)
        self.assertIn("GLD moved +6.0%", state["days"][-1]["trigger"])

    def test_portfolio_fall_wakes_it_early(self):
        broker, state = self._decided(review=10)
        broker.prices = {s: 90.0 for s in config.UNIVERSE}  # 70% invested: about -7%
        broker.day = "2026-11-17"
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), state)
        self.assertIn("portfolio fell", state["days"][-1]["trigger"])
        self.assertFalse(state["halted"])

    def test_unusable_answer_is_retried_next_day(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says(None, status="bad_output"), None)
        broker.day = "2026-11-17"
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), state)
        self.assertIn("retry", state["days"][-1]["trigger"])
        self.assertEqual(state["days"][-1]["decision_status"], "ok")

    def test_review_days_are_kept_between_1_and_10(self):
        self.assertEqual(triggers.clamp_review_days(0), 1)
        self.assertEqual(triggers.clamp_review_days(40), 10)
        self.assertEqual(triggers.clamp_review_days("x"), 1)
        self.assertEqual(triggers.add_trading_days("2026-11-13", 1), "2026-11-16")  # Friday -> Monday

    # --- v2: inputs -----------------------------------------------------------------------
    def test_prompt_has_news_economy_and_scorecard(self):
        broker, state = self._decided(review=1)
        broker.day = "2026-11-17"
        broker.prices["SPY"] = 102.0
        ai = ai_says({"SPY": 0.5, "GLD": 0.2})
        run.run_once(broker, ai, state, economy=lambda today: "- VIX volatility index: 15.00 on 2026-11-16")
        prompt = ai.calls[0][1]
        self.assertIn("Fed held rates", prompt)
        self.assertIn("VIX volatility index", prompt)
        self.assertIn("Scorecard", prompt)
        self.assertIn("2026-11-16 to 2026-11-17", prompt)
        self.assertNotIn("999.00", prompt)

    def test_all_samples_see_the_same_prompt(self):
        broker = FakeBroker()
        ai = ai_says({"SPY": 0.5})
        run.run_once(broker, ai, None)
        self.assertEqual(len(ai.calls), config.ENSEMBLE_SIZE)
        self.assertEqual(len({c for c in ai.calls}), 1)
        self.assertEqual(len(ai.research_calls), 1)

    def test_shadows_on_dashboard(self):
        broker, state = self._decided()
        broker.day = "2026-11-17"
        broker.prices["SPY"] = 110.0
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "GLD": 0.2}), state)
        run.write_report(state, broker.equity(), 110.0, "2026-11-17")
        data = json.loads(Path(config.DASHBOARD_DATA_PATH).read_text())
        for name in shadows.SHADOWS:
            self.assertIsNotNone(data["series"][-1][name])
        self.assertAlmostEqual(sum(data["api_cost_split"].values()), state["api_cost_usd"], places=4)
        self.assertIn("Simple rules", Path(config.REPORT_PATH).read_text())


class EnsembleTest(unittest.TestCase):
    def test_median_of_three(self):
        a = Decision("ok", {"SPY": 0.5, "TLT": 0.3}, "a", 0.1, next_review_days=2)
        b = Decision("ok", {"SPY": 0.4, "TLT": 0.1}, "b", 0.1, next_review_days=5)
        c = Decision("ok", {"SPY": 0.0, "GLD": 0.5}, "c", 0.1, next_review_days=10)
        d = brain.combine([a, b, c])
        self.assertEqual(d.status, "ok")
        self.assertAlmostEqual(d.weights["SPY"], 0.4)
        self.assertAlmostEqual(d.weights["TLT"], 0.1)
        self.assertAlmostEqual(d.weights["GLD"], 0.0)
        self.assertEqual(d.next_review_days, 5)
        self.assertEqual(d.rationale, "b")
        self.assertAlmostEqual(d.cost_usd, 0.3)

    def test_median_total_never_above_one(self):
        d = brain.combine([Decision("ok", {"SPY": .5, "QQQ": .5}, "", 0), Decision("ok", {"SPY": .5, "GLD": .5}, "", 0),
                           Decision("ok", {"QQQ": .5, "GLD": .5}, "", 0)])
        self.assertLessEqual(sum(d.weights.values()), 1.0 + 1e-9)

    def test_one_bad_answer_is_tolerated_two_are_not(self):
        ok = Decision("ok", {"SPY": 0.5}, "r", 0.1)
        bad = Decision("api_error", None, "", 0.0, "timeout")
        self.assertEqual(brain.combine([ok, bad, ok]).status, "ok")
        self.assertEqual(brain.combine([ok, bad, bad]).status, "no_consensus")
        self.assertEqual(brain.combine([]).status, "no_consensus")

    def test_search_fees_are_counted(self):
        usage = {"input_tokens": 0, "output_tokens": 0, "server_tool_use": {"web_search_requests": 4}}
        self.assertAlmostEqual(brain.cost_of_usage(usage), 4 * config.WEB_SEARCH_USD)

    def test_schema_asks_for_review_days(self):
        self.assertIn("next_review_days", brain.output_schema()["required"])


class ShadowTest(unittest.TestCase):
    def _day(self, date_, spy=100.0, trend=()):
        prices = {s: 100.0 for s in config.UNIVERSE}
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
        return [{"headline": "Fed holds", "url": "javascript:alert(1)", "symbols": ["SPY", "AAPL"],
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
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "GLD": 0.2}), None)
        broker.prices["SPY"] = 110.0
        broker.last_equity = 100_000.0
        broker.filled = [order("1", "SPY", "buy", 500, 100, "2026-11-16T15:00:00Z")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live.json"
            self.assertIn("updated", live.refresh(broker, state, path, "2026-11-16T15:30:00Z"))
            data = json.loads(path.read_text())
            spy = next(p for p in data["positions"] if p["symbol"] == "SPY")
            self.assertAlmostEqual(spy["pl_real_usd"], 500 * 10 * 0.01)
            self.assertAlmostEqual(spy["weight"], 55_000 / 105_000)
            self.assertAlmostEqual(data["account"]["today_real_usd"], 50.0)
            self.assertEqual(data["headlines"][0]["url"], "")              # unsafe link removed
            self.assertEqual(data["headlines"][0]["symbols"], ["SPY"])
            self.assertEqual(len(data["intraday"]), 2)                     # empty point skipped
            self.assertEqual(live.refresh(broker, state, path, "2026-11-16T15:45:00Z"), "No change.")
            broker.fail_news = True
            broker.prices["SPY"] = 111.0
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
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}), None)
        self.assertEqual(state["days"][0]["orders"][0]["order_id"], "1")


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
        drivers = [{"factor": "Fed hike odds up", "input": "news", "etfs": ["TLT"], "effect": "lower"}]
        answer = Decision("ok", {"SPY": 0.5}, "why", 0.1, next_review_days=1, drivers=drivers)
        broker = FakeBroker()
        state, _ = run.run_once(broker, FakeAI([answer]), None, economy=lambda t: "- VIX: 15")
        broker.day, broker.prices["SPY"] = "2026-11-17", 104.0
        state, _ = run.run_once(broker, FakeAI([answer]), state)
        state["risk_free_rate"] = 0.04
        run.write_report(state, broker.equity(), 104.0, "2026-11-17")
        data = json.loads(Path(config.DASHBOARD_DATA_PATH).read_text())
        self.assertEqual(data["metrics"]["ai_after_costs"]["days"], 2)
        self.assertEqual(data["risk_free_rate"], 0.04)
        first = data["log"][-1]
        self.assertEqual(first["drivers"], drivers)
        self.assertEqual(first["economy"], "- VIX: 15")
        self.assertEqual(first["weights_before"]["SPY"], 0.0)
        row = data["scorecard"][0]
        self.assertAlmostEqual(row["added"], row["ai"] - row["keep"])


class DriversAndSourcesTest(unittest.TestCase):
    def test_drivers_are_cleaned(self):
        raw = [{"factor": " Yields up ", "input": "rumour", "etfs": ["TLT", "AAPL"], "effect": "sell!"},
               {"factor": "", "input": "news", "etfs": [], "effect": "raise"}, "junk"] + \
              [{"factor": f"f{i}", "input": "prices", "etfs": [], "effect": "hold"} for i in range(20)]
        out = brain.clean_drivers(raw)
        self.assertEqual(out[0], {"factor": "Yields up", "input": "other", "etfs": ["TLT"], "effect": "hold"})
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
