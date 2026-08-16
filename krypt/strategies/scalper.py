"""HFT-style scalper.

NOT true microsecond HFT (impossible from a retail Python REST client). This is
high-frequency-STYLE scalping: it reacts to order-book imbalance + micro-momentum
on the fastest candles and aims for small, fast moves with tight risk.

Signal logic:
  - order-book imbalance (bid vs ask depth) is the primary edge
  - confirmed by short-EMA micro-momentum (EMA9 vs EMA21 on 1m)
  - only trades when the spread is tight (cost control)
  - exits on mean reversion / opposite imbalance (handled by app stop/TP)
"""

from __future__ import annotations

from .base import Strategy
from .. import indicators


class ScalperHFT(Strategy):
    name = "scalper"

    IMBALANCE_ENTER = 0.25     # |imbalance| above this is a signal
    MAX_SPREAD_BPS = 5.0       # skip if spread wider than 5 basis points
    DEPTH = 20

    def evaluate(self, symbol):
        ob = self.client.book_imbalance(symbol, self.DEPTH)
        mid = ob["mid"]
        intents = []
        tele = {"strategy": self.name, "symbol": symbol, **ob}

        if mid is None or mid <= 0:
            return intents, tele

        spread_bps = ob["spread"] / mid * 1e4
        tele["spread_bps"] = round(spread_bps, 2)
        if spread_bps > self.MAX_SPREAD_BPS:
            tele["skip"] = f"spread {spread_bps:.1f}bps too wide"
            return intents, tele

        # micro-momentum confirmation from fast candles
        candles = self.client.klines(symbol, "1m", 60)
        closes = [c["close"] for c in candles]
        e9 = indicators.ema(closes, 9)[-1]
        e21 = indicators.ema(closes, 21)[-1]
        mom_up = e9 is not None and e21 is not None and e9 > e21
        mom_dn = e9 is not None and e21 is not None and e9 < e21
        tele["ema9"], tele["ema21"] = e9, e21

        imb = ob["imbalance"]
        if imb > self.IMBALANCE_ENTER and mom_up:
            intents.append({"symbol": symbol, "side": "BUY", "notional_usd": None,
                            "reason": f"scalp long: imb {imb:+.2f} + EMA9>EMA21"})
        elif imb < -self.IMBALANCE_ENTER and mom_dn:
            intents.append({"symbol": symbol, "side": "FLAT", "notional_usd": None,
                            "reason": f"scalp exit: imb {imb:+.2f} + EMA9<EMA21"})
        return intents, tele
