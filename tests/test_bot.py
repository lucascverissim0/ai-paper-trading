"""Offline tests: a fake broker and a fake AI, no network, no install needed.

Run with:  python3 -m unittest tests.test_bot
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from bot import accounting, brain, config, run
from bot.brain import Decision
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

    def clock(self):
        return {"is_open": self.is_open, "timestamp": f"{self.day}T11:30:00-05:00"}

    def positions(self):
        return {s: q * self.prices[s] for s, q in self.shares.items() if q}

    def equity(self):
        return self.cash_ + sum(self.positions().values())

    def cash(self):
        return self.cash_

    def latest_price(self, symbol):
        return self.prices[symbol]

    def daily_closes(self, symbols, start_iso):
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


def ai_says(weights, cost=0.10, status="ok"):
    calls = []

    def decide(system, prompt):
        calls.append((system, prompt))
        return Decision(status, weights if status == "ok" else None, "test reason", cost)

    decide.calls = calls
    return decide


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
        config.REPORT_PATH = os.path.join(self.tmp.name, "report.md")

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_day_trades_and_records_costs(self):
        broker = FakeBroker()
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5, "GLD": 0.2}), None)
        self.assertEqual(state["days"][0]["decision_status"], "ok")
        self.assertAlmostEqual(state["api_cost_usd"], 0.10)
        self.assertGreater(state["fees_real_usd"], 0)
        self.assertAlmostEqual(broker.positions()["SPY"], 50_000, delta=1)
        run.write_report(state, broker.equity(), 100.0, state["days"][-1]["date"])
        self.assertIn("after all costs", Path(config.REPORT_PATH).read_text())

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
        state, _ = run.run_once(broker, ai_says({"SPY": 0.5}, cost=config.API_BUDGET_USD - 0.01), None)
        broker.day = "2026-11-17"
        ai = ai_says({"SPY": 0.0})
        state, _ = run.run_once(broker, ai, state)
        self.assertEqual(ai.calls, [])
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
        state, _ = run.run_once(broker, ai_says(None, cost=0.05, status="refused"), None)
        self.assertEqual(broker.orders, [])
        self.assertAlmostEqual(state["api_cost_usd"], 0.05)

    def test_cost_saved_before_trading(self):
        saved = []
        run.run_once(FakeBroker(), ai_says({"SPY": 0.5}), None, save=lambda s: saved.append(s["api_cost_usd"]))
        self.assertEqual(saved, [0.10])

    def test_no_live_trading_address_in_code(self):
        source = (Path(run.__file__).parent / "alpaca.py").read_text()
        self.assertNotIn("https://api.alpaca.markets", source)
        self.assertIn("https://paper-api.alpaca.markets", source)


if __name__ == "__main__":
    unittest.main()
