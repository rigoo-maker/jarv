"""TopstepX / ProjectX Gateway REST client (stdlib only).

!! VERIFY BEFORE LIVE USE !!
Every endpoint path and enum value lives in the WIRE block below, deliberately
in one place, because this was written without network access to the gateway
(this sandbox's egress policy blocks api.topstepx.com). The shapes follow the
published ProjectX Gateway API, but vendors rename fields. Run `doctor`, then
correct this one block if anything 404s or returns an unexpected shape — no
other module hardcodes a path.

Auth: POST an API key once, get a session JWT (~24h), revalidate before expiry.
Never put the key in code. It comes from the environment only.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# --------------------------------------------------------------------------
# WIRE BLOCK — the only place endpoints and enums are written down.
# --------------------------------------------------------------------------
BASE_URL = os.environ.get("TOPSTEP_API_URL", "https://api.topstepx.com")
RTC_URL = os.environ.get("TOPSTEP_RTC_URL", "https://rtc.topstepx.com")

EP = {
    "login_key":      "/api/Auth/loginKey",
    "validate":       "/api/Auth/validate",
    "account_search": "/api/Account/search",
    "contract_search": "/api/Contract/search",
    "bars":           "/api/History/retrieveBars",
    "order_place":    "/api/Order/place",
    "order_cancel":   "/api/Order/cancel",
    "order_modify":   "/api/Order/modify",
    "order_open":     "/api/Order/searchOpen",
    "position_open":  "/api/Position/searchOpen",
    "position_close": "/api/Position/closeContract",
    "trade_search":   "/api/Trade/search",
}

ORDER_TYPE = {"limit": 1, "market": 2, "stop": 4, "trailing_stop": 5,
              "join_bid": 6, "join_ask": 7}
SIDE = {"buy": 0, "sell": 1}          # 0 = Bid/buy, 1 = Ask/sell
BAR_UNIT = {"second": 1, "minute": 2, "hour": 3, "day": 4,
            "week": 5, "month": 6}
# --------------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, msg, status=None, body=None):
        self.status, self.body = status, (body or "")[:500]
        super().__init__(msg)


class BlockedError(RuntimeError):
    """Host unreachable — network policy, DNS or firewall. Not a code bug."""


class TopstepClient:
    def __init__(self, username=None, api_key=None, base_url=None, timeout=30.0):
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.username = username or os.environ.get("TOPSTEP_USERNAME", "")
        self._api_key = api_key or os.environ.get("TOPSTEP_API_KEY", "")
        self.timeout = timeout
        self.token = None
        self.token_at = 0.0

    # --- transport ---------------------------------------------------------
    def _post(self, path, payload=None, auth=True):
        url = f"{self.base_url}{path}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if auth:
            if not self.token:
                self.login()
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(payload or {}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode() or "null")
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode()
            except Exception:
                pass
            raise ApiError(f"HTTP {e.code} for {path}", e.code, text)
        except urllib.error.URLError as e:
            raise BlockedError(f"cannot reach {url} ({e.reason}). If this is a "
                               f"proxy CONNECT failure the host is blocked by "
                               f"network policy — run where it is reachable.")
        # The gateway wraps results: {"success":bool,"errorCode":int,
        # "errorMessage":str, ...payload}
        if isinstance(data, dict) and data.get("success") is False:
            raise ApiError(f"{path} failed: "
                           f"{data.get('errorMessage') or data.get('errorCode')}",
                           body=json.dumps(data))
        return data

    # --- auth --------------------------------------------------------------
    def login(self):
        if not self.username or not self._api_key:
            raise ApiError("set TOPSTEP_USERNAME and TOPSTEP_API_KEY in the "
                           "environment (never in code)")
        data = self._post(EP["login_key"],
                          {"userName": self.username, "apiKey": self._api_key},
                          auth=False)
        self.token = (data or {}).get("token")
        if not self.token:
            raise ApiError("login returned no token", body=json.dumps(data))
        self.token_at = time.time()
        return self.token

    def ensure_token(self, max_age_s=20 * 3600):
        """Revalidate before the ~24h session token expires."""
        if not self.token:
            return self.login()
        if time.time() - self.token_at > max_age_s:
            try:
                data = self._post(EP["validate"], {})
                new = (data or {}).get("newToken")
                if new:
                    self.token, self.token_at = new, time.time()
            except ApiError:
                self.login()
        return self.token

    # --- reference data ----------------------------------------------------
    def accounts(self, only_active=True):
        d = self._post(EP["account_search"], {"onlyActiveAccounts": only_active})
        return (d or {}).get("accounts") or []

    def search_contracts(self, text, live=False):
        d = self._post(EP["contract_search"], {"searchText": text, "live": live})
        return (d or {}).get("contracts") or []

    def bars(self, contract_id, *, unit="minute", unit_number=1, limit=500,
             start=None, end=None, live=False, include_partial=False):
        """Historical bars -> krypt candle format, oldest first."""
        payload = {
            "contractId": contract_id,
            "live": live,
            "unit": BAR_UNIT[unit],
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": include_partial,
        }
        if start:
            payload["startTime"] = start
        if end:
            payload["endTime"] = end
        d = self._post(EP["bars"], payload)
        return [_to_candle(b) for b in ((d or {}).get("bars") or [])][::-1]

    # --- trading -----------------------------------------------------------
    def place_order(self, account_id, contract_id, *, side, size,
                    order_type="market", limit_price=None, stop_price=None,
                    trail_price=None, tag=None, linked_order_id=None):
        payload = {
            "accountId": account_id,
            "contractId": contract_id,
            "type": ORDER_TYPE[order_type],
            "side": SIDE[side],
            "size": int(size),
            "limitPrice": limit_price,
            "stopPrice": stop_price,
            "trailPrice": trail_price,
            "customTag": tag,
            "linkedOrderId": linked_order_id,
        }
        return self._post(EP["order_place"],
                          {k: v for k, v in payload.items() if v is not None})

    def cancel_order(self, account_id, order_id):
        return self._post(EP["order_cancel"],
                          {"accountId": account_id, "orderId": order_id})

    def modify_order(self, account_id, order_id, **fields):
        return self._post(EP["order_modify"],
                          {"accountId": account_id, "orderId": order_id, **fields})

    def open_orders(self, account_id):
        d = self._post(EP["order_open"], {"accountId": account_id})
        return (d or {}).get("orders") or []

    def open_positions(self, account_id):
        d = self._post(EP["position_open"], {"accountId": account_id})
        return (d or {}).get("positions") or []

    def close_position(self, account_id, contract_id):
        return self._post(EP["position_close"],
                          {"accountId": account_id, "contractId": contract_id})

    def trades(self, account_id, start=None, end=None):
        p = {"accountId": account_id}
        if start:
            p["startTimestamp"] = start
        if end:
            p["endTimestamp"] = end
        d = self._post(EP["trade_search"], p)
        return (d or {}).get("trades") or []


def _to_candle(b):
    """Gateway bar -> krypt candle. Tolerates both short and long field names."""
    def pick(*names, default=None):
        for n in names:
            if n in b and b[n] is not None:
                return b[n]
        return default
    ts = pick("t", "timestamp")
    return {
        "time": _to_ms(ts),
        "open": float(pick("o", "open", default=0) or 0),
        "high": float(pick("h", "high", default=0) or 0),
        "low": float(pick("l", "low", default=0) or 0),
        "close": float(pick("c", "close", default=0) or 0),
        "volume": float(pick("v", "volume", default=0) or 0),
    }


def _to_ms(ts):
    if ts is None:
        return 0
    if isinstance(ts, (int, float)):
        return int(ts if ts > 1e11 else ts * 1000)
    s = str(ts).replace("Z", "+00:00")
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return 0
