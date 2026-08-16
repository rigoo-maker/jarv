"""Trend follower.

Slower strategy that trades with the dominant trend using the full signal-scoring
engine. Enters long on a strong bull score with trend confirmation (ADX), exits
on score reversal. Lower turnover than the scalper; meant to ride bigger moves.
"""

from __future__ import annotations

from .base import Strategy
from .. import indicators
from ..scoring import score_snapshot


class TrendFollower(Strategy):
    name = "trend"

    ENTER_SCORE = 35
    EXIT_SCORE = -10

    def evaluate(self, symbol):
        candles = self.client.klines(symbol, self.cfg.interval, 200)
        snap = indicators.compute_all(candles)["latest"]
        sc = score_snapshot(snap)
        intents = []
        tele = {"strategy": self.name, "symbol": symbol, "price": snap["price"],
                "score": sc["score"], "label": sc["label"], "adx": snap["adx"]}

        adx_ok = (snap.get("adx") or 0) >= 20
        if sc["score"] >= self.ENTER_SCORE and adx_ok:
            intents.append({"symbol": symbol, "side": "BUY", "notional_usd": None,
                            "reason": f"trend long: score {sc['score']} {sc['label']}"})
        elif sc["score"] <= self.EXIT_SCORE:
            intents.append({"symbol": symbol, "side": "FLAT", "notional_usd": None,
                            "reason": f"trend exit: score {sc['score']} {sc['label']}"})
        return intents, tele
