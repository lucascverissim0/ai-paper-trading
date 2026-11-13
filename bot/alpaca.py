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

    # -- Orders ---------------------------------------------------------------------
    def cancel_open_orders(self) -> None:
        self._trade("DELETE", "/v2/orders")

    def market_order(self, symbol: str, side: str, notional: float) -> dict:
        body = {
            "symbol": symbol,
            "notional": f"{notional:.2f}",
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        return self._trade("POST", "/v2/orders", body=body)

    def close_position(self, symbol: str) -> dict:
        return self._trade("DELETE", f"/v2/positions/{symbol}")

    def close_all(self) -> None:
        self._trade("DELETE", "/v2/positions", params={"cancel_orders": "true"})

    def wait_final(self, order_id: str, timeout_s: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout_s
        order = self._trade("GET", f"/v2/orders/{order_id}")
        while order["status"] not in FINAL_ORDER_STATUSES and time.monotonic() < deadline:
            time.sleep(2)
            order = self._trade("GET", f"/v2/orders/{order_id}")
        return order

    # -- Market data --------------------------------------------------------------
    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        data = self._data("/v2/stocks/trades/latest", {"symbols": ",".join(symbols), "feed": FEED})
        return {s: float(data["trades"][s]["p"]) for s in symbols}

    def latest_price(self, symbol: str) -> float:
        return self.latest_prices([symbol])[symbol]

    def daily_closes(self, symbols: list[str], start_iso: str) -> dict[str, list[tuple[str, float]]]:
        """Symbol -> [(YYYY-MM-DD, close)], oldest first, adjusted for splits and dividends."""
        out: dict[str, list[tuple[str, float]]] = {s: [] for s in symbols}
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "start": start_iso,
            "adjustment": "all",
            "feed": FEED,
            "limit": 10000,
        }
        while True:
            data = self._data("/v2/stocks/bars", params)
            for sym, bars in (data.get("bars") or {}).items():
                out.setdefault(sym, []).extend((b["t"][:10], float(b["c"])) for b in bars)
            token = data.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        for sym in out:
            out[sym].sort()
        return out
