"""One daily run: check the market, ask Claude, trade on the paper account, update the report.

Run with:  python -m bot.run
"""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import accounting, brain, config
from .portfolio import PlannedOrder, clean_weights, fit_buys_to_cash, plan_orders

SYSTEM_PROMPT = """You are the decision-maker in a paper-trading experiment (simulated money, nothing real).
Once per US trading day you set target weights for a long-only portfolio of the ETFs listed in the
message. Any weight you leave unallocated is held as cash.

Rules enforced by code whatever you answer: each weight between 0 and {max_w:.2f}, total at most 1.0,
and changes smaller than {min_trade:.0%} of the portfolio are ignored.

Goal: beat simply buying and holding SPY over the whole test, measured AFTER all costs.
Costs at the real size of ${real:,.0f}: each order costs about ${fee_order:.2f} plus {fee_rate:.2%} of the amount
traded, and each decision you make costs about ${api_call:.2f} in API fees. Frequent trading is expensive;
keeping the current weights is often the best decision. You only see prices: no news.

Answer with the JSON requested: a weight for every ETF (0 for none) and a rationale of at most
three sentences."""


# --- helpers -----------------------------------------------------------------------
def load_state(path: str) -> dict | None:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else None


def save_state(path: str, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(p)


def write_report(state: dict, equity: float, bench_price: float, today: str) -> None:
    summary = accounting.summarize(state, equity, bench_price, today)
    Path(config.REPORT_PATH).write_text(accounting.render_report(summary, state))
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    payload = accounting.dashboard_payload(state, equity, bench_price, today, updated_at)
    data_path = Path(config.DASHBOARD_DATA_PATH)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(json.dumps(payload, indent=1))


def _stats(closes: list[float]) -> str:
    last = closes[-1]

    def ret(n: int) -> str:
        return f"{(last / closes[-1 - n] - 1) * 100:+.1f}%" if len(closes) > n else "n/a"

    daily = [closes[i] / closes[i - 1] - 1 for i in range(max(1, len(closes) - 20), len(closes))]
    vol = f"{statistics.pstdev(daily) * math.sqrt(252) * 100:.1f}%" if len(daily) >= 2 else "n/a"
    ma200 = f"{(last / statistics.mean(closes[-200:]) - 1) * 100:+.1f}%" if len(closes) >= 200 else "n/a"
    return f"1m {ret(21)}, 3m {ret(63)}, 6m {ret(126)}, 12m {ret(252)}, 20-day vol {vol}, vs 200-day avg {ma200}"


def build_prompt(today: str, closes: dict, weights_now: dict, state: dict, equity: float, bench_price: float) -> str:
    parts = [f"Today is {today}. Data below uses completed trading days only (adjusted closes)."]
    parts.append("\nETFs:")
    for s in config.UNIVERSE:
        series = [c for _, c in closes[s]]
        recent = ", ".join(f"{c:.2f}" for c in series[-config.PROMPT_CLOSES:])
        parts.append(f"- {s} ({config.UNIVERSE_DESCRIPTIONS[s]}): {_stats(series)}")
        parts.append(f"  last {min(len(series), config.PROMPT_CLOSES)} closes: {recent}")

    held = ", ".join(f"{s} {w * 100:.1f}%" for s, w in weights_now.items() if w > 0.0005) or "nothing"
    parts.append(f"\nCurrent portfolio: {held}; cash is the rest.")

    summary = accounting.summarize(state, equity, bench_price, today)
    parts.append(
        f"Test so far ({summary['days_elapsed']} days): you {summary['gross_return'] * 100:+.2f}% before costs, "
        f"SPY {summary['bench_return'] * 100:+.2f}%. After all costs you are "
        f"{'ahead' if summary['edge_usd'] >= 0 else 'behind'} by ${abs(summary['edge_usd']):.2f} "
        f"at ${config.REAL_CAPITAL_USD:,.0f}."
    )
    recent = [d for d in state["days"] if d.get("decision_status") == "ok"][-config.RECENT_DECISIONS:]
    if recent:
        parts.append("\nYour recent decisions:")
        for d in recent:
            w = ", ".join(f"{k} {v * 100:.0f}%" for k, v in d["weights"].items() if v > 0) or "all cash"
            parts.append(f"- {d['date']}: {w}. {d['rationale'][:200]}")
    return "\n".join(parts)


def _system_prompt(api_call_estimate: float) -> str:
    return SYSTEM_PROMPT.format(
        max_w=config.MAX_WEIGHT,
        min_trade=config.MIN_TRADE_FRACTION,
        real=config.REAL_CAPITAL_USD,
        fee_order=config.FEE_PER_ORDER_USD,
        fee_rate=config.FEE_RATE,
        api_call=api_call_estimate,
    )


def _filled_notional(order: dict) -> float:
    try:
        return float(order.get("filled_qty") or 0) * float(order.get("filled_avg_price") or 0)
    except (TypeError, ValueError):
        return 0.0


def execute(broker, orders: list[PlannedOrder], paper_equity: float) -> tuple[list[dict], float]:
    """Place sells, then buys sized to the cash actually available. Returns records and real-size fees."""
    records: list[dict] = []
    fees = 0.0

    def place(o: PlannedOrder) -> None:
        nonlocal fees
        try:
            placed = broker.close_position(o.symbol) if o.close_all else broker.market_order(o.symbol, o.side, o.notional)
            final = broker.wait_final(placed["id"])
            filled = _filled_notional(final)
            status = final["status"]
        except Exception as e:  # one failed order must not stop the others
            filled, status = 0.0, f"error: {e}"[:200]
        fee = accounting.fee_real(filled, paper_equity)
        fees += fee
        records.append({
            "symbol": o.symbol,
            "side": o.side,
            "requested": "all" if o.close_all else o.notional,
            "filled_usd": round(filled, 2),
            "status": status,
            "fee_real_usd": round(fee, 4),
        })

    sells = [o for o in orders if o.side == "sell"]
    buys = [o for o in orders if o.side == "buy"]
    for o in sells:
        place(o)
    if buys:
        for o in fit_buys_to_cash(buys, broker.cash()):
            place(o)
    return records, fees


# --- the daily run -------------------------------------------------------------------
def run_once(broker, decide, state: dict | None, save=lambda s: None) -> tuple[dict | None, str]:
    """Returns the new state (None if the test has not started) and a one-line log message.

    `save` is called as soon as API money is spent, so a crash later can never lose that cost.
    """
    clock = broker.clock()
    if not clock["is_open"]:
        return state, "Market closed: nothing to do."
    today = str(clock["timestamp"])[:10]  # New York date
    if state and any(d["date"] == today for d in state["days"]):
        return state, f"Already ran on {today}: nothing to do."

    equity = broker.equity()
    bench_price = broker.latest_price(config.BENCHMARK)
    if state is None:
        state = {
            "version": 1,
            "start_date": today,
            "start_equity": equity,
            "bench_start_price": bench_price,
            "peak_equity": equity,
            "api_cost_usd": 0.0,
            "fees_real_usd": 0.0,
            "halted": False,
            "halt_reason": None,
            "days": [],
        }
    state["peak_equity"] = max(state["peak_equity"], equity)
    day = {"date": today, "equity": equity, "bench_price": bench_price, "orders": [], "api_cost_usd": 0.0}

    def finish(status: str, message: str) -> tuple[dict, str]:
        day["decision_status"] = status
        state["days"].append(day)
        return state, message

    if state["halted"]:
        return finish("halted", f"Test is stopped ({state['halt_reason']}): recording value only.")

    if equity < state["peak_equity"] * (1 - config.MAX_DRAWDOWN):
        held = broker.positions()
        broker.close_all()
        fees = sum(accounting.fee_real(abs(v), equity) for v in held.values())
        state["fees_real_usd"] += fees
        day["orders"] = [{"symbol": s, "side": "sell", "requested": "all", "filled_usd": round(abs(v), 2),
                          "status": "sell-all sent", "fee_real_usd": round(accounting.fee_real(abs(v), equity), 4)}
                         for s, v in held.items()]
        state["halted"] = True
        state["halt_reason"] = f"value fell more than {config.MAX_DRAWDOWN:.0%} from its peak; everything sold"
        return finish("halted", "Loss limit hit: sold everything and stopped the test.")

    positions = broker.positions()
    weights_now = {s: positions.get(s, 0.0) / equity for s in config.UNIVERSE}
    start = (date.fromisoformat(today) - timedelta(days=config.HISTORY_CALENDAR_DAYS)).isoformat() + "T00:00:00Z"
    closes = broker.daily_closes(config.UNIVERSE, start)
    closes = {s: [(d, c) for d, c in rows if d < today] for s, rows in closes.items()}
    missing = [s for s in config.UNIVERSE if len(closes.get(s, [])) < 2]
    if missing:
        return finish("no_data", f"No price history for {', '.join(missing)}: holding.")

    prompt = build_prompt(today, closes, weights_now, state, equity, bench_price)
    draft_system = _system_prompt(0.0)
    worst = brain.worst_case_cost(draft_system, prompt)
    if state["api_cost_usd"] + worst > config.API_BUDGET_USD:
        return finish("budget_reached", "API budget reached: holding current positions, no AI call.")

    average_cost = state["api_cost_usd"] / max(1, sum(1 for d in state["days"] if d["api_cost_usd"] > 0))
    decision = decide(_system_prompt(average_cost or 0.10), prompt)
    state["api_cost_usd"] += decision.cost_usd
    day["api_cost_usd"] = decision.cost_usd
    save(state)
    if decision.status != "ok":
        return finish(decision.status, f"No usable AI answer ({decision.status} {decision.detail}): holding.")

    target = clean_weights(decision.weights)
    day["weights"] = {k: round(v, 4) for k, v in target.items()}
    day["rationale"] = decision.rationale
    broker.cancel_open_orders()
    orders = plan_orders(target, positions, equity)
    day["orders"], fees = execute(broker, orders, equity)
    state["fees_real_usd"] += fees
    return finish("ok", f"Decision made, {len(orders)} order(s) placed, API cost ${decision.cost_usd:.4f}.")


def main() -> None:
    from .alpaca import AlpacaPaper

    config.check()
    broker = AlpacaPaper(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""))
    state = load_state(config.STATE_PATH)
    days_before = len(state["days"]) if state else 0

    def save(s: dict) -> None:
        save_state(config.STATE_PATH, s)

    new_state, message = run_once(broker, brain.decide, state, save)
    print(message)
    if new_state is not None and len(new_state["days"]) > days_before:
        save(new_state)
        write_report(new_state, broker.equity(), broker.latest_price(config.BENCHMARK), new_state["days"][-1]["date"])


if __name__ == "__main__":
    main()
