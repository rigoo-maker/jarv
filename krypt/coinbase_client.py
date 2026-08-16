"""Coinbase Advanced Trade client — execution venue.

Coinbase Advanced Trade (via the Coinbase Developer Platform) authenticates with
short-lived JWTs signed ES256 by your EC private key — NOT Binance-style HMAC.
So this needs `PyJWT` and `cryptography` (pip install PyJWT cryptography).

Credentials come from the environment ONLY (never hardcoded, never committed):
  COINBASE_API_KEY_NAME   e.g. organizations/<org>/apiKeys/<key-uuid>
  COINBASE_API_PRIVATE_KEY  the -----BEGIN EC PRIVATE KEY----- PEM block
                            (use \n for newlines if storing on one line)

Public market data (price, order book, candles) is also available via Coinbase's
public endpoints with no auth.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.request
import urllib.error

API_HOST = "api.coinbase.com"
API_BASE = f"https://{API_HOST}"


class CoinbaseError(RuntimeError):
    pass


def symbol_to_product(symbol: str) -> str:
    """BTCUSDT -> BTC-USD, ETHUSDT -> ETH-USD, BTCUSD -> BTC-USD."""
    s = symbol.upper()
    for quote in ("USDT", "USDC", "USD"):
        if s.endswith(quote):
            return f"{s[:-len(quote)]}-USD"
    return s


def _build_jwt(key_name: str, private_key_pem: str, method: str, path: str) -> str:
    try:
        import jwt  # PyJWT
        from cryptography.hazmat.primitives import serialization
    except Exception as e:  # ImportError or broken backend
        raise CoinbaseError(
            "Coinbase live trading needs PyJWT + cryptography. "
            "Run: pip install PyJWT cryptography  (original error: %s)" % e)

    pem = private_key_pem.replace("\\n", "\n").encode()
    private_key = serialization.load_pem_private_key(pem, password=None)
    now = int(time.time())
    uri = f"{method} {API_HOST}{path}"
    payload = {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120, "uri": uri}
    headers = {"kid": key_name, "nonce": secrets.token_hex()}
    return jwt.encode(payload, private_key, algorithm="ES256", headers=headers)


class CoinbaseClient:
    def __init__(self, cfg):
        self.cfg = cfg

    # ---------------- auth'd requests ----------------

    def _signed(self, method, path, body=None):
        if not (self.cfg.cb_key_name and self.cfg.cb_private_key):
            raise CoinbaseError("COINBASE_API_KEY_NAME / COINBASE_API_PRIVATE_KEY "
                                "not set in environment")
        token = _build_jwt(self.cfg.cb_key_name, self.cfg.cb_private_key, method, path)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            API_BASE + path, data=data, method=method,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json",
                     "User-Agent": "krypt/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise CoinbaseError(f"HTTP {e.code} {method} {path}: {e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            raise CoinbaseError(f"network error reaching Coinbase: {e}")

    def _public(self, path):
        req = urllib.request.Request(API_BASE + path, headers={"User-Agent": "krypt/0.1"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.URLError as e:
            raise CoinbaseError(f"network error reaching Coinbase: {e}")

    # ---------------- market data ----------------

    def price(self, symbol):
        prod = symbol_to_product(symbol)
        d = self._public(f"/api/v3/brokerage/market/products/{prod}")
        return float(d.get("price"))

    # ---------------- account & orders ----------------

    def accounts(self):
        return self._signed("GET", "/api/v3/brokerage/accounts")

    def balances(self):
        out = {}
        for a in self.accounts().get("accounts", []):
            bal = a.get("available_balance", {})
            v = float(bal.get("value", 0) or 0)
            if v > 0:
                out[bal.get("currency", a.get("currency"))] = v
        return out

    def new_order(self, symbol, side, type_="MARKET", quantity=None, quote_qty=None,
                  price=None, time_in_force="GTC"):
        """Place an order on Coinbase Advanced Trade. Market BUY uses quote_size
        (USD to spend); market SELL uses base_size (BTC to sell)."""
        prod = symbol_to_product(symbol)
        cfgur = {}
        if type_ == "MARKET":
            if side.upper() == "BUY":
                if quote_qty is None:
                    raise CoinbaseError("market BUY needs quote_qty (USD)")
                cfgur = {"market_market_ioc": {"quote_size": f"{quote_qty:.2f}"}}
            else:
                if quantity is None:
                    raise CoinbaseError("market SELL needs quantity (base size)")
                cfgur = {"market_market_ioc": {"base_size": f"{quantity:.8f}"}}
        else:  # LIMIT
            cfgur = {"limit_limit_gtc": {
                "base_size": f"{quantity:.8f}", "limit_price": f"{price:.2f}"}}
        body = {
            "client_order_id": secrets.token_hex(16),
            "product_id": prod,
            "side": side.upper(),
            "order_configuration": cfgur,
        }
        return self._signed("POST", "/api/v3/brokerage/orders", body)
