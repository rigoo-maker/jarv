"""Execution layer.

Routes intents through the risk engine, then either:
  - analyze : logs the intent only (no fill)
  - paper   : simulates a fill at the given price
  - live    : places a REAL signed order via BinanceClient

Every intent and fill is appended to an audit log (JSONL). Live trading is
gated by Config.assert_live_allowed() (the two-lock check).
"""

from __future__ import annotations

import json
import os
import time


class Trader:
    def __init__(self, cfg, client, risk, audit_path="krypt_audit.jsonl"):
        self.cfg = cfg
        self.client = client
        self.risk = risk
        self.audit_path = audit_path
        cfg.assert_live_allowed()  # fail fast before any trading happens

    def _audit(self, record):
        record["ts"] = int(time.time() * 1000)
        record["mode"] = self.cfg.mode
        record["testnet"] = self.cfg.testnet
        try:
            with open(self.audit_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass
        return record

    def execute(self, symbol, side, price, notional_usd=None, reason=""):
        """Attempt a trade. Returns a result dict (never raises on rejection)."""
        side = side.upper()
        if notional_usd is None:
            notional_usd = self.risk.position_size_usd(price)

        ok, why = self.risk.check_order(symbol, side, notional_usd)
        if not ok:
            return self._audit({"event": "rejected", "symbol": symbol, "side": side,
                                "notional_usd": round(notional_usd, 2),
                                "reason": reason, "risk": why})

        qty = notional_usd / price if price else 0.0

        if self.cfg.mode == "analyze":
            return self._audit({"event": "signal_only", "symbol": symbol, "side": side,
                                "price": price, "qty": round(qty, 8),
                                "notional_usd": round(notional_usd, 2), "reason": reason})

        if self.cfg.mode == "paper":
            realized = self.risk.on_fill(symbol, side, qty, price)
            return self._audit({"event": "paper_fill", "symbol": symbol, "side": side,
                                "price": price, "qty": round(qty, 8),
                                "notional_usd": round(notional_usd, 2),
                                "realized_pnl": round(realized, 4), "reason": reason})

        # ---- live ----
        try:
            if side == "BUY":
                resp = self.client.new_order(symbol, "BUY", "MARKET",
                                             quote_qty=notional_usd)
            else:
                resp = self.client.new_order(symbol, "SELL", "MARKET", quantity=qty)
            # best-effort fill price/qty from response
            fill_price = price
            fill_qty = qty
            fills = resp.get("fills") or []
            if fills:
                tot_q = sum(float(f["qty"]) for f in fills)
                tot_c = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                if tot_q:
                    fill_price = tot_c / tot_q
                    fill_qty = tot_q
            realized = self.risk.on_fill(symbol, side, fill_qty, fill_price)
            return self._audit({"event": "live_fill", "symbol": symbol, "side": side,
                                "price": fill_price, "qty": round(fill_qty, 8),
                                "notional_usd": round(fill_qty * fill_price, 2),
                                "realized_pnl": round(realized, 4),
                                "order_id": resp.get("orderId"), "reason": reason})
        except Exception as e:
            return self._audit({"event": "live_error", "symbol": symbol, "side": side,
                                "error": str(e), "reason": reason})

    def flatten(self, symbol, price):
        """Close any open position in `symbol` (paper/live)."""
        pos = self.risk.positions.get(symbol)
        if not pos or abs(pos.qty) < 1e-12:
            return None
        return self.execute(symbol, "SELL", price,
                            notional_usd=abs(pos.qty) * price, reason="flatten")
