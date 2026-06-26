"""Strategy base class.

A strategy reads market data and returns a list of intents:
    {"symbol", "side": BUY|SELL|FLAT, "notional_usd"|None, "reason"}
The app loop passes each intent to Trader.execute(). Strategies NEVER place
orders directly — execution and risk gating live in Trader/RiskEngine.
"""

from __future__ import annotations


class Strategy:
    name = "base"

    def __init__(self, cfg, client):
        self.cfg = cfg
        self.client = client

    def evaluate(self, symbol):
        """Return (intents, telemetry). Override in subclasses."""
        raise NotImplementedError
