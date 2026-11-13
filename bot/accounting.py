"""Profitability after ALL costs, restated at the real capital Lucas would invest."""
from __future__ import annotations

from datetime import date

from . import config


def fee_real(filled_notional_paper: float, paper_equity: float) -> float:
    """Fee one filled order would cost at real size: fixed fee plus a percentage."""
    if filled_notional_paper <= 0:
        return 0.0
    scale = config.REAL_CAPITAL_USD / paper_equity
    return config.FEE_PER_ORDER_USD + config.FEE_RATE * filled_notional_paper * scale


def max_drawdown(values: list[float]) -> float:
    peak, worst = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, 1 - v / peak)
    return worst


def summarize(state: dict, equity_now: float, bench_now: float, today: str) -> dict:
    real = config.REAL_CAPITAL_USD
    gross = equity_now / state["start_equity"] - 1
    bench = bench_now / state["bench_start_price"] - 1
    days = (date.fromisoformat(today) - date.fromisoformat(state["start_date"])).days
    other = config.OTHER_MONTHLY_COSTS_USD * days / 30.44
    api = state["api_cost_usd"]
    fees = state["fees_real_usd"]
    bench_fees = config.FEE_PER_ORDER_USD + config.FEE_RATE * real  # one purchase at the start

    strat_net = real * gross - fees - api - other
    bench_net = real * bench - bench_fees

    equity_series = [d["equity"] for d in state["days"]] + [equity_now]
    bench_series = [d["bench_price"] for d in state["days"]] + [bench_now]
    return {
        "today": today,
        "days_elapsed": days,
        "decisions": sum(1 for d in state["days"] if d.get("decision_status") == "ok"),
        "gross_return": gross,
        "bench_return": bench,
        "fees_real": fees,
        "bench_fees": bench_fees,
        "api_cost": api,
        "other_costs": other,
        "strat_net_usd": strat_net,
        "bench_net_usd": bench_net,
        "edge_usd": strat_net - bench_net,
        "strat_drawdown": max_drawdown(equity_series),
        "bench_drawdown": max_drawdown(bench_series),
    }


def dashboard_payload(state: dict, equity_now: float, bench_now: float, today: str, updated_at: str) -> dict:
    """Everything the web dashboard shows, already computed here so the page only displays it."""
    real = config.REAL_CAPITAL_USD
    start = date.fromisoformat(state["start_date"])
    bench_fees = config.FEE_PER_ORDER_USD + config.FEE_RATE * real

    # One point per day: the value measured at each run, and for today the value after today's trades.
    points = [(d["date"], d["equity"], d["bench_price"]) for d in state["days"]]
    if points and points[-1][0] == today:
        points[-1] = (today, equity_now, bench_now)
    else:
        points.append((today, equity_now, bench_now))

    series = []
    fees_cum = api_cum = 0.0
    day_costs = {}
    for d in state["days"]:
        fees = sum(o.get("fee_real_usd", 0.0) for o in d.get("orders", []))
        prev = day_costs.get(d["date"], (0.0, 0.0))
        day_costs[d["date"]] = (prev[0] + fees, prev[1] + d.get("api_cost_usd", 0.0))
    for day, equity, bench in points:
        fees, api = day_costs.pop(day, (0.0, 0.0))
        fees_cum += fees
        api_cum += api
        other = config.OTHER_MONTHLY_COSTS_USD * (date.fromisoformat(day) - start).days / 30.44
        series.append({
            "date": day,
            "ai_before_costs": round(real * equity / state["start_equity"], 2),
            "ai_after_costs": round(real * equity / state["start_equity"] - fees_cum - api_cum - other, 2),
            "spy_after_costs": round(real * bench / state["bench_start_price"] - bench_fees, 2),
            "fees_cum": round(fees_cum, 4),
            "api_cum": round(api_cum, 4),
        })

    trades, log = [], []
    for d in state["days"]:
        scale = real / d["equity"] if d["equity"] else 0.0
        for o in d.get("orders", []):
            trades.append({
                "date": d["date"],
                "symbol": o["symbol"],
                "side": o["side"],
                "amount_real_usd": round(o.get("filled_usd", 0.0) * scale, 2),
                "fee_real_usd": round(o.get("fee_real_usd", 0.0), 4),
                "status": o.get("status", ""),
            })
        log.append({
            "date": d["date"],
            "status": d.get("decision_status", ""),
            "api_cost_usd": round(d.get("api_cost_usd", 0.0), 4),
            "weights": d.get("weights"),
            "rationale": d.get("rationale", ""),
        })

    last = next((d for d in reversed(state["days"]) if d.get("decision_status") == "ok"), None)
    return {
        "updated_at": updated_at,
        "status": "stopped" if state.get("halted") else "running",
        "halt_reason": state.get("halt_reason"),
        "start_date": state["start_date"],
        "settings": {
            "real_capital_usd": real,
            "api_budget_usd": config.API_BUDGET_USD,
            "fee_per_order_usd": config.FEE_PER_ORDER_USD,
            "fee_rate": config.FEE_RATE,
            "model": config.CLAUDE_MODEL,
            "max_drawdown": config.MAX_DRAWDOWN,
            "universe": config.UNIVERSE_DESCRIPTIONS,
        },
        "summary": summarize(state, equity_now, bench_now, today),
        "holdings": last["weights"] if last else {},
        "series": series,
        "trades": trades[::-1],
        "log": log[::-1],
    }


def render_report(s: dict, state: dict) -> str:
    real = config.REAL_CAPITAL_USD
    status = f"STOPPED - {state['halt_reason']}" if state.get("halted") else "running"
    verdict = "AHEAD of" if s["edge_usd"] > 0 else "BEHIND"
    last = next((d for d in reversed(state["days"]) if d.get("decision_status") == "ok"), None)

    def pct(x: float) -> str:
        return f"{x * 100:+.2f}%"

    def usd(x: float) -> str:
        return f"{'-' if x < 0 else ''}${abs(x):,.2f}"

    lines = [
        "# AI paper-trading test - results",
        "",
        f"Last update: {s['today']} - status: {status}",
        "",
        "Simulated money only. No real money is used or at risk.",
        "",
        f"Results restated as if ${real:,.0f} had been invested.",
        "",
        "| | AI strategy | Buy and hold SPY |",
        "|---|---|---|",
        f"| Return before costs | {pct(s['gross_return'])} | {pct(s['bench_return'])} |",
        f"| Trading fees (modelled) | {usd(s['fees_real'])} | {usd(s['bench_fees'])} |",
        f"| Claude API cost (measured) | {usd(s['api_cost'])} | $0.00 |",
        f"| Other costs | {usd(s['other_costs'])} | $0.00 |",
        f"| Gain after all costs | {usd(s['strat_net_usd'])} ({pct(s['strat_net_usd'] / real)}) "
        f"| {usd(s['bench_net_usd'])} ({pct(s['bench_net_usd'] / real)}) |",
        f"| Worst fall from a peak | {pct(-s['strat_drawdown'])} | {pct(-s['bench_drawdown'])} |",
        "",
        f"**Verdict so far: the AI is {verdict} buy-and-hold by {usd(abs(s['edge_usd']))} after all costs.**",
        "",
        f"Days since start: {s['days_elapsed']} - AI decisions made: {s['decisions']} - "
        f"API budget used: {usd(s['api_cost'])} of {usd(config.API_BUDGET_USD)}.",
        "",
        "Under 12 months, a result either way can be luck. Dividends are left out on both sides.",
    ]
    if last:
        held = ", ".join(f"{k} {v * 100:.0f}%" for k, v in last["weights"].items() if v > 0) or "all cash"
        lines += ["", f"Last decision ({last['date']}): {held}.", "", f"Reason given: {last['rationale']}"]
    return "\n".join(lines) + "\n"
