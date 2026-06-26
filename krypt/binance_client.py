"""Binance REST client — stdlib only (urllib + hmac).

Public endpoints (klines, order book, ticker) need no key. Signed endpoints
(account, order) use HMAC-SHA256 over the query string with X-MBX-APIKEY.
Supports spot and USDT-M futures, mainnet and testnet.

Honors HTTPS_PROXY automatically via urllib. If your network blocks Binance,
every call raises BinanceError with the blocked host — that is a policy issue,
not a bug.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
import urllib.parse
import urllib.request
import urllib.error


class BinanceError(RuntimeError):
    pass


class BinanceClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self._filters_cache = {}

    # ---------------- low-level HTTP ----------------

    def _request(self, base, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"User-Agent": "krypt/0.1"}
        if signed:
            if not (self.cfg.api_key and self.cfg.api_secret):
                raise BinanceError("signed request needs API key/secret")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urllib.parse.urlencode(params)
            sig = hmac.new(self.cfg.api_secret.encode(), query.encode(),
                           hashlib.sha256).hexdigest()
            query += "&signature=" + sig
            headers["X-MBX-APIKEY"] = self.cfg.api_key
            url = f"{base}{path}?{query}"
            data = None
            if method in ("POST", "PUT", "DELETE"):
                data = b""  # params already in query string (Binance accepts this)
        else:
            query = urllib.parse.urlencode(params)
            url = f"{base}{path}" + (f"?{query}" if query else "")
            data = None

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise BinanceError(f"HTTP {e.code} {method} {path}: {body}")
        except urllib.error.URLError as e:
            raise BinanceError(f"network error reaching {base} (blocked/down?): {e}")

    # ---------------- public market data ----------------

    def ping(self):
        return self._request(self.cfg.spot_base, "GET", "/api/v3/ping")

    def klines(self, symbol, interval, limit=300):
        raw = self._request(self.cfg.spot_base, "GET", "/api/v3/klines",
                            {"symbol": symbol, "interval": interval, "limit": limit})
        return [{
            "time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]), "volume": float(c[5]),
        } for c in raw]

    def order_book(self, symbol, limit=20):
        d = self._request(self.cfg.spot_base, "GET", "/api/v3/depth",
                          {"symbol": symbol, "limit": limit})
        bids = [(float(p), float(q)) for p, q in d["bids"]]
        asks = [(float(p), float(q)) for p, q in d["asks"]]
        return {"bids": bids, "asks": asks}

    def ticker_price(self, symbol):
        d = self._request(self.cfg.spot_base, "GET", "/api/v3/ticker/price",
                          {"symbol": symbol})
        return float(d["price"])

    def book_imbalance(self, symbol, limit=20):
        """Order-book pressure in [-1,+1]; >0 = more bid (buy) depth."""
        ob = self.order_book(symbol, limit)
        bid_vol = sum(q for _, q in ob["bids"])
        ask_vol = sum(q for _, q in ob["asks"])
        tot = bid_vol + ask_vol
        imb = (bid_vol - ask_vol) / tot if tot else 0.0
        spread = ob["asks"][0][0] - ob["bids"][0][0] if ob["asks"] and ob["bids"] else 0.0
        mid = ((ob["asks"][0][0] + ob["bids"][0][0]) / 2
               if ob["asks"] and ob["bids"] else None)
        return {"imbalance": imb, "spread": spread, "mid": mid,
                "best_bid": ob["bids"][0][0] if ob["bids"] else None,
                "best_ask": ob["asks"][0][0] if ob["asks"] else None}

    # ---------------- symbol filters (lot size / min notional) ----------------

    def symbol_filters(self, symbol):
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        d = self._request(self.cfg.spot_base, "GET", "/api/v3/exchangeInfo",
                          {"symbol": symbol})
        info = d["symbols"][0]
        f = {flt["filterType"]: flt for flt in info["filters"]}
        step = float(f.get("LOT_SIZE", {}).get("stepSize", "0.00000001"))
        min_qty = float(f.get("LOT_SIZE", {}).get("minQty", "0"))
        tick = float(f.get("PRICE_FILTER", {}).get("tickSize", "0.01"))
        min_notional = float(f.get("NOTIONAL", f.get("MIN_NOTIONAL", {})).get("minNotional", "0")
                             or 0)
        out = {"step": step, "min_qty": min_qty, "tick": tick,
               "min_notional": min_notional}
        self._filters_cache[symbol] = out
        return out

    @staticmethod
    def _round_step(value, step):
        if step <= 0:
            return value
        precision = max(0, abs(int(round(-1 * (len(str(step).split(".")[1].rstrip("0")) if "." in str(step) else 0)))))
        n = (value // step) * step
        return round(n, 8 if precision == 0 else precision)

    def round_qty(self, symbol, qty):
        step = self.symbol_filters(symbol)["step"]
        # floor to step
        if step > 0:
            qty = (int(qty / step)) * step
        return float(f"{qty:.8f}".rstrip("0").rstrip(".") or "0")

    # ---------------- account & orders (signed) ----------------

    def account(self):
        return self._request(self.cfg.spot_base, "GET", "/api/v3/account", signed=True)

    def balances(self):
        acct = self.account()
        return {b["asset"]: float(b["free"]) for b in acct.get("balances", [])
                if float(b["free"]) > 0 or float(b["locked"]) > 0}

    def new_order(self, symbol, side, type_="MARKET", quantity=None,
                  quote_qty=None, price=None, time_in_force="GTC"):
        """Place a REAL order (signed). Caller is responsible for safety checks."""
        params = {"symbol": symbol, "side": side, "type": type_}
        if quantity is not None:
            params["quantity"] = self.round_qty(symbol, quantity)
        if quote_qty is not None:
            params["quoteOrderQty"] = round(quote_qty, 2)
        if type_ == "LIMIT":
            params["price"] = price
            params["timeInForce"] = time_in_force
        return self._request(self.cfg.spot_base, "POST", "/api/v3/order",
                             params, signed=True)

    def cancel_all(self, symbol):
        return self._request(self.cfg.spot_base, "DELETE", "/api/v3/openOrders",
                             {"symbol": symbol}, signed=True)
