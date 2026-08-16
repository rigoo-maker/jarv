"""Delta hedge.

Reduces directional risk by offsetting a spot position with an opposite futures
position (delta-neutral), or by signalling a hedge when the trend regime flips
against an open position.

Two uses:
  1. Static hedge ratio: hold `hedge_ratio` of notional short on USDT-M futures
     against spot longs, so price drops are largely offset.
  2. Dynamic: when ADX shows a strong opposite trend, raise the hedge.

This module computes the TARGET hedge and emits an intent describing it. Actually
opening futures positions requires futures API permissions; in analyze/paper it
is logged. Hedging reduces downside but also caps upside and costs funding.
"""

from __future__ import annotations

from .base import Strategy
from .. import indicators


class DeltaHedge(Strategy):
    name = "hedge"

    BASE_HEDGE_RATIO = 0.5     # hedge half the spot notional by default

    def evaluate(self, symbol):
        candles = self.client.klines(symbol, self.cfg.interval, 200)
        snap = indicators.compute_all(candles)["latest"]
        intents = []
        tele = {"strategy": self.name, "symbol": symbol,
                "price": snap["price"], "adx": snap["adx"]}

        ratio = self.BASE_HEDGE_RATIO
        adx = snap.get("adx") or 0
        pdi, mdi = snap.get("plus_di") or 0, snap.get("minus_di") or 0
        # strong downtrend -> hedge more; strong uptrend -> hedge less
        if adx >= 25:
            if mdi > pdi:
                ratio = min(1.0, ratio + 0.3)
                tele["regime"] = "strong down -> increase hedge"
            else:
                ratio = max(0.0, ratio - 0.3)
                tele["regime"] = "strong up -> reduce hedge"
        else:
            tele["regime"] = "weak trend -> base hedge"

        tele["target_hedge_ratio"] = round(ratio, 2)
        intents.append({
            "symbol": symbol, "side": "HEDGE", "notional_usd": None,
            "hedge_ratio": round(ratio, 2),
            "reason": f"target delta hedge {ratio:.0%} (ADX {adx:.0f})",
        })
        return intents, tele
