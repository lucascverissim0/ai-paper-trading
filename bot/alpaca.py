"""Minimal client for Alpaca's PAPER trading API (simulated money).

The live-trading address is deliberately absent: this code cannot place a real trade.
Only the Python standard library is used.
"""
from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
FEED = "iex"  # free data feed

FINAL_ORDER_STATUSES = {"filled", "canceled", "expired", "rejected", "done_for_day"}


class AlpacaError(RuntimeError):
    pass


class AlpacaPaper:
    def __init__(self, key_id: str, secret_key: str):
        if not key_id or not secret_key:
            raise SystemExit("Alpaca paper keys are missing (ALPACA_KEY_ID / ALPACA_SECRET_KEY).")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- HTTP -----------------------------------------------------------------
    def _call(self, method: str, url: str, params: dict | None = None, body: dict | None = None):
        if params:
            url = f"{url}?{urlencode(params)}"
        data = json.dumps(body).encode() if body is not None else None
        attempts = 3 if method == "GET" else 1  # never repeat an order by accident
        for attempt in range(attempts):
            req = Request(url, data=data, method=method, headers=self._headers)
            try:
                with urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if method == "GET" and (e.code == 429 or e.code >= 500) and attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise AlpacaError(f"{method} {url.split('?')[0]} failed with {e.code}: {detail}") from None
            except URLError as e:
                if method == "GET" and attempt < attempts - 1:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise AlpacaError(f"{method} {url.split('?')[0]} failed: {e.reason}") from None
        raise AlpacaError("unreachable")

    def _trade(self, method: str, path: str, **kw):
        return self._call(method, PAPER_TRADING_URL + path, **kw)

    def _data(self, path: str, params: dict):
        return self._call("GET", DATA_URL + path, params=params)

    # -- Account ----------------------------------------------------------------
    def clock(self) -> dict:
        return self._trade("GET", "/v2/clock")

    def equity(self) -> float:
        return float(self._trade("GET", "/v2/account")["equity"])

    def cash(self) -> float:
        return float(self._trade("GET", "/v2/account")["cash"])

    def positions(self) -> dict[str, float]:
        """Symbol -> market value in dollars (long positions only are ever opened)."""
        return {p["symbol"]: float(p["market_value"]) for p in self._trade("GET", "/v2/positions")}

    # -- Read-only views for the dashboard ---------------------------------------
    def account(self) -> dict:
        return self._trade("GET", "/v2/account")

    def positions_detail(self) -> list[dict]:
        return self._trade("GET", "/v2/positions")

    def filled_orders(self, after_iso: str) -> list[dict]:
        """Closed orders submitted after `after_iso`, oldest first (callers keep those with a fill)."""
        out: dict[str, dict] = {}
        after = after_iso
        for _ in range(20):  # 500 per page; far more than this test will ever place
            page = self._trade("GET", "/v2/orders", params={
                "status": "closed", "after": after, "direction": "asc", "limit": 500})
            for o in page:
                out[o["id"]] = o
            if len(page) < 500:
                break
            after = page[-1]["submitted_at"]
        return sorted(out.values(), key=lambda o: o.get("submitted_at") or "")

    def portfolio_history(self, period: str = "1D", timeframe: str = "15Min") -> dict:
        return self._trade("GET", "/v2/account/portfolio/history", params={"period": period, "timeframe": timeframe})

    def news(self, symbols: list[str], limit: int = 50) -> list[dict]:
        """Latest headlines tagged with these symbols (free Alpaca news feed), newest first."""
        data = self._data("/v1beta1/news", {"symbols": ",".join(symbols), "limit": limit, "sort": "desc"})
        return data.get("news") or []

    # -- Orders ---------------------------------------------------------------------
    def cancel_open_orders(self, timeout_s: float = 30.0) -> None:
        """Cancel every open order (stop-losses included) and wait until none is left, so the shares
        they reserved can be sold or covered by a new stop."""
        self._trade("DELETE", "/v2/orders")
        deadline = time.monotonic() + timeout_s
        while self._trade("GET", "/v2/orders", params={"status": "open", "limit": 500}) and time.monotonic() < deadline:
            time.sleep(1)

    def market_order(self, symbol: str, side: str, qty: int) -> dict:
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "market", "time_in_force": "day"}
        return self._trade("POST", "/v2/orders", body=body)

    def stop_order(self, symbol: str, qty: int, stop_price: float) -> dict:
        """Sell `qty` shares if the price trades at or below `stop_price`; stays open until cancelled."""
        body = {"symbol": symbol, "qty": str(int(qty)), "side": "sell", "type": "stop",
                "stop_price": f"{stop_price:.2f}", "time_in_force": "gtc"}
        return self._trade("POST", "/v2/orders", body=body)

    def close_position(self, symbol: str) -> dict:
        return self._trade("DELETE", f"/v2/positions/{symbol}")

    def wait_final(self, order_id: str, timeout_s: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout_s
        order = self._trade("GET", f"/v2/orders/{order_id}")
        while order["status"] not in FINAL_ORDER_STATUSES and time.monotonic() < deadline:
            time.sleep(2)
            order = self._trade("GET", f"/v2/orders/{order_id}")
        return order

    # -- Market data --------------------------------------------------------------
    def assets(self) -> list[dict]:
        """Every active US equity Alpaca lists (stocks, ETFs, ADRs), tradable or not."""
        return self._trade("GET", "/v2/assets", params={"status": "active", "asset_class": "us_equity"})

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        """Last trade price per symbol (symbols without a recent trade are left out)."""
        out: dict[str, float] = {}
        symbols = sorted(set(symbols))
        for i in range(0, len(symbols), 100):
            data = self._data("/v2/stocks/trades/latest", {"symbols": ",".join(symbols[i:i + 100]), "feed": FEED})
            for s, t in (data.get("trades") or {}).items():
                if t and t.get("p"):
                    out[s] = float(t["p"])
        return out

    def latest_price(self, symbol: str) -> float:
        return self.latest_prices([symbol])[symbol]

    def daily_bars(self, symbols: list[str], start_iso: str, end_iso: str, feed: str) -> dict[str, list]:
        """Symbol -> [(YYYY-MM-DD, close, volume)], oldest first, adjusted for splits and dividends."""
        out: dict[str, list] = {}
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start_iso,
            "end": end_iso,
            "adjustment": "all",
            "feed": feed,
            "limit": 10000,
        }
        while True:
            data = self._data("/v2/stocks/bars", params)
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend((b["t"][:10], float(b["c"]), float(b.get("v") or 0)) for b in bars)
            token = data.get("next_page_token")
            time.sleep(0.35)  # stay under the free plan's 200 requests a minute
            if not token:
                break
            params["page_token"] = token
        for sym in out:
            out[sym].sort()
        return out
