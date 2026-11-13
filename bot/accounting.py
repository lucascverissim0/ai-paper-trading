"""Profitability after ALL costs, restated at the real capital Lucas would invest."""
from __future__ import annotations

from datetime import date

from . import config, shadows, triggers

SHADOW_LABELS = shadows.SHADOWS


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


def shadow_gains(state: dict) -> dict[str, float]:
    """Gain in dollars after fees of each simple-rule portfolio, at real capital."""
    out = {}
    for name, points in shadows.all_shadows(state["days"]).items():
        if points:
            out[name] = round(points[-1]["value"] - config.REAL_CAPITAL_USD, 2)
    return out


def scorecard(state: dict, equity_now: float, prices_now: dict, bench_now: float, today: str) -> list[dict]:
    """For each valid decision, until the next one (or now): the AI's result, keeping the previous
    holdings instead, and SPY. Before costs; the decision's own fees and API cost are listed apart."""
    decisions = [d for d in state["days"] if d.get("decision_status") == "ok" and d.get("prices")]
    rows = []
    for i, d in enumerate(decisions):
        if i + 1 < len(decisions):
            nxt = decisions[i + 1]
            end_date, end_equity, end_prices, end_bench = nxt["date"], nxt["equity"], nxt["prices"], nxt["bench_price"]
        else:
            end_date, end_equity, end_prices, end_bench = today, equity_now, prices_now, bench_now
        if end_date <= d["date"] or not end_prices:
            continue
        before = d.get("weights_before") or {}
        keep = sum(w * (end_prices[s] / d["prices"][s] - 1) for s, w in before.items()
                   if w > 0 and s in end_prices and s in d["prices"])
        fees = sum(o.get("fee_real_usd", 0.0) for o in d.get("orders", []))
        rows.append({
            "from": d["date"],
            "to": end_date,
            "ai": end_equity / d["equity"] - 1,
            "keep": keep,
            "spy": end_bench / d["bench_price"] - 1,
            "cost_usd": round(fees + d.get("api_cost_usd", 0.0), 4),
            "held": ", ".join(f"{k} {v * 100:.0f}%" for k, v in (d.get("weights") or {}).items() if v > 0) or "all cash",
            "rationale": d.get("rationale", ""),
        })
    return rows


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
        "shadows": shadow_gains(state),
        "next_review": triggers.next_review_date(state),
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
    shadow_values = {name: {p["date"]: p["value"] for p in pts}
                     for name, pts in shadows.all_shadows(state["days"]).items()}
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
            **{name: values.get(day) for name, values in shadow_values.items()},
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
            "trigger": d.get("trigger", ""),
            "next_review_days": d.get("next_review_days"),
            "briefing": d.get("briefing", ""),
            "briefing_status": d.get("briefing_status", ""),
            "searches": d.get("searches", 0),
            "samples": d.get("samples", []),
            "cost_split": d.get("cost_split", {}),
        })

    last = next((d for d in reversed(state["days"]) if d.get("decision_status") == "ok"), None)
    split = {"research": 0.0, "searches": 0.0, "decisions": 0.0}
    for d in state["days"]:
        for k, v in d.get("cost_split", {}).items():
            split[k] = split.get(k, 0.0) + v
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
        "shadow_labels": SHADOW_LABELS,
        "api_cost_split": {k: round(v, 4) for k, v in split.items()},
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
    if s["shadows"]:
        lines += ["", "Simple rules on the same days, prices and fees (no AI cost):", "",
                  "| Rule | Gain after fees |", "|---|---|"]
        lines += [f"| {SHADOW_LABELS[k]} | {usd(v)} ({pct(v / real)}) |" for k, v in s["shadows"].items()]
    if s["next_review"]:
        lines += ["", f"Next review chosen by the AI: {s['next_review']} (earlier if markets jolt)."]
    if last:
        held = ", ".join(f"{k} {v * 100:.0f}%" for k, v in last["weights"].items() if v > 0) or "all cash"
        lines += ["", f"Last decision ({last['date']}): {held}.", "", f"Reason given: {last['rationale']}"]
    return "\n".join(lines) + "\n"
