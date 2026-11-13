"""Near-live view of the paper account for the dashboard: open positions and their P&L, every trade with
its realised P&L, today's value curve and free headlines. Read-only: no order, no Claude call, no cost.

Run with:  python -m bot.live   (every ~15 minutes during US market hours, and after each daily run)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import accounting, config


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_url(url) -> str:
    """Only plain web links reach the page."""
    return url if isinstance(url, str) and url.startswith(("https://", "http://")) else ""


def _scale(state: dict) -> float:
    """Paper dollars -> dollars at the real capital, the same scale as the 'AI before costs' line."""
    return config.REAL_CAPITAL_USD / state["start_equity"]


def trades_from_orders(orders: list[dict], state: dict) -> tuple[list[dict], dict]:
    """Every filled order since the test began, oldest first, with its modelled fee and, for sales,
    the realised P&L against the average purchase price. Amounts at real capital."""
    scale = _scale(state)
    fee_by_id, fee_by_key, reason_by_date, equity_by_date = {}, {}, {}, {}
    for d in state["days"]:
        reason_by_date[d["date"]] = d.get("rationale", "")
        equity_by_date[d["date"]] = d.get("equity") or state["start_equity"]
        for o in d.get("orders", []):
            if o.get("order_id"):
                fee_by_id[o["order_id"]] = o.get("fee_real_usd", 0.0)
            fee_by_key.setdefault((d["date"], o["symbol"], o["side"]), []).append(o.get("fee_real_usd", 0.0))

    fills = []
    for o in orders:
        qty, price = _f(o.get("filled_qty")), _f(o.get("filled_avg_price"))
        when = o.get("filled_at") or o.get("updated_at") or o.get("submitted_at") or ""
        if qty <= 0 or price <= 0 or when[:10] < state["start_date"]:
            continue
        fills.append((when, o, qty, price))
    fills.sort(key=lambda f: f[0])

    held: dict[str, list[float]] = {}  # symbol -> [shares, total cost]
    rows = []
    for when, o, qty, price in fills:
        day, symbol, side = when[:10], o.get("symbol", ""), o.get("side", "")
        if o.get("id") in fee_by_id:
            fee = fee_by_id[o["id"]]
        elif fee_by_key.get((day, symbol, side)):
            fee = fee_by_key[(day, symbol, side)].pop(0)
        else:
            fee = accounting.fee_real(qty * price, equity_by_date.get(day, state["start_equity"]))
        shares, cost = held.get(symbol, [0.0, 0.0])
        realized = realized_pct = None
        if side == "buy":
            held[symbol] = [shares + qty, cost + qty * price]
        elif side == "sell" and shares > 0:
            avg = cost / shares
            sold = min(qty, shares)
            realized, realized_pct = (price - avg) * sold * scale, price / avg - 1
            held[symbol] = [shares - sold, cost - avg * sold]
        rows.append({
            "time": when,
            "date": day,
            "symbol": symbol,
            "side": side,
            "qty": round(qty, 6),
            "price": round(price, 4),
            "amount_real_usd": round(qty * price * scale, 2),
            "fee_real_usd": round(fee, 4),
            "realized_real_usd": None if realized is None else round(realized, 2),
            "realized_pct": realized_pct,
            "reason": reason_by_date.get(day, "")[:400],
        })

    sells = [r for r in rows if r["realized_real_usd"] is not None]
    avg_equity = sum(equity_by_date.values()) / len(equity_by_date) if equity_by_date else state["start_equity"]
    stats = {
        "trades": len(rows),
        "buys": sum(1 for r in rows if r["side"] == "buy"),
        "sells": sum(1 for r in rows if r["side"] == "sell"),
        "realized_real_usd": round(sum(r["realized_real_usd"] for r in sells), 2),
        "winning_sells": sum(1 for r in sells if r["realized_real_usd"] > 0),
        "closed_sells": len(sells),
        "fees_real_usd": round(sum(r["fee_real_usd"] for r in rows), 4),
        "turnover": sum(q * p for _, _, q, p in fills) / avg_equity if avg_equity else 0.0,
    }
    return rows, stats


def positions_view(raw: list[dict], equity: float, state: dict) -> list[dict]:
    scale = _scale(state)
    rows = []
    for p in raw:
        if abs(_f(p.get("qty"))) < 1e-9:
            continue  # fully sold
        value = _f(p.get("market_value"))
        rows.append({
            "symbol": p.get("symbol", ""),
            "name": config.UNIVERSE_DESCRIPTIONS.get(p.get("symbol", ""), ""),
            "qty": _f(p.get("qty")),
            "avg_entry_price": _f(p.get("avg_entry_price")),
            "price": _f(p.get("current_price")),
            "weight": value / equity if equity else 0.0,
            "value_real_usd": round(value * scale, 2),
            "pl_real_usd": round(_f(p.get("unrealized_pl")) * scale, 2),
            "pl_pct": _f(p.get("unrealized_plpc")),
            "today_real_usd": round(_f(p.get("unrealized_intraday_pl")) * scale, 2),
            "today_pct": _f(p.get("change_today")),
        })
    return sorted(rows, key=lambda r: -r["value_real_usd"])


def intraday_view(history: dict, state: dict) -> list[dict]:
    scale = _scale(state)
    points = []
    for t, equity in zip(history.get("timestamp") or [], history.get("equity") or []):
        if equity:
            when = datetime.fromtimestamp(int(t), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            points.append({"time": when, "value_real_usd": round(_f(equity) * scale, 2)})
    return points


def headlines_view(news: list[dict]) -> list[dict]:
    rows = []
    for n in news[: config.HEADLINES_LIMIT]:
        rows.append({
            "time": n.get("created_at", ""),
            "headline": str(n.get("headline", ""))[:300],
            "summary": str(n.get("summary", ""))[:400],
            "source": str(n.get("source", ""))[:60],
            "url": safe_url(n.get("url")),
            "symbols": [s for s in (n.get("symbols") or []) if s in config.UNIVERSE],
        })
    return rows


def build_live(broker, state: dict, clock: dict, now: str) -> dict:
    """Each part is fetched on its own: one failing part is reported, the others still show."""
    payload = {
        "refreshed_at": now,
        "market_open": bool(clock.get("is_open")),
        "next_open": clock.get("next_open"),
        "next_close": clock.get("next_close"),
        "errors": [],
    }
    scale = _scale(state)

    def part(name: str, fn):
        try:
            return fn()
        except Exception as e:  # shown on the page in plain words
            payload["errors"].append(f"{name} unavailable ({type(e).__name__})")
            return None

    account = part("account", broker.account)
    if account:
        equity, last = _f(account.get("equity")), _f(account.get("last_equity"))
        payload["account"] = {
            "value_real_usd": round(equity * scale, 2),
            "today_real_usd": round((equity - last) * scale, 2) if last else None,
            "today_pct": equity / last - 1 if last else None,
            "cash_weight": _f(account.get("cash")) / equity if equity else None,
        }
        positions = part("positions", lambda: positions_view(broker.positions_detail(), equity, state))
        if positions is not None:
            payload["positions"] = positions
            payload["account"]["open_pl_real_usd"] = round(sum(p["pl_real_usd"] for p in positions), 2)
    orders = part("trades", lambda: broker.filled_orders(state["start_date"] + "T00:00:00Z"))
    if orders is not None:
        payload["trades"], payload["trade_stats"] = trades_from_orders(orders, state)
        payload["trades"].reverse()  # newest first
    intraday = part("today's curve", lambda: intraday_view(broker.portfolio_history(), state))
    if intraday is not None:
        payload["intraday"] = intraday
    news = part("headlines", lambda: headlines_view(broker.news(config.UNIVERSE, config.HEADLINES_LIMIT)))
    if news is not None:
        payload["headlines"] = news
    return payload


def _content(payload: dict | None) -> dict | None:
    return None if payload is None else {k: v for k, v in payload.items() if k != "refreshed_at"}


def refresh(broker, state: dict | None, path: Path, now: str) -> str:
    if not state:
        return "Test not started: nothing to show."
    old = json.loads(path.read_text()) if path.exists() else None
    clock = broker.clock()
    if not clock.get("is_open") and old and old.get("market_open") is False:
        return "Market closed and closing values already saved."
    payload = build_live(broker, state, clock, now)
    if _content(payload) == _content(old):
        return "No change."
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1))
    return "Live view updated" + (f" ({'; '.join(payload['errors'])})" if payload["errors"] else "") + "."


def main() -> None:
    from .alpaca import AlpacaPaper

    state_path = Path(config.STATE_PATH)
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    broker = AlpacaPaper(os.environ.get("ALPACA_KEY_ID", ""), os.environ.get("ALPACA_SECRET_KEY", ""))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(refresh(broker, state, Path(config.LIVE_DATA_PATH), now))


if __name__ == "__main__":
    main()
