"""Engine — one evaluation tick producing dashboard state + executing intents."""

from __future__ import annotations

from . import indicators
from .scoring import score_snapshot


class Engine:
    def __init__(self, cfg, client, strategy, trader, risk, alerts):
        self.cfg = cfg
        self.client = client
        self.strategy = strategy
        self.trader = trader
        self.risk = risk
        self.alerts = alerts

    def tick(self, symbol):
        """Evaluate one symbol: data -> indicators -> score -> strategy -> execute.
        Returns the dashboard state dict for this symbol."""
        candles = self.client.klines(symbol, self.cfg.interval, 200)
        ind = indicators.compute_all(candles)
        snap = ind["latest"]
        scoring = score_snapshot(snap)

        try:
            book = self.client.book_imbalance(symbol)
        except Exception:
            book = {}

        # strategy intents (executed through the risk-gated trader)
        executed = []
        try:
            intents, _tele = self.strategy.evaluate(symbol)
            for it in intents:
                price = snap["price"]
                if it["side"] == "FLAT":
                    res = self.trader.flatten(symbol, price)
                elif it["side"] in ("BUY", "SELL"):
                    res = self.trader.execute(symbol, it["side"], price,
                                              it.get("notional_usd"), it.get("reason", ""))
                else:  # HEDGE / informational
                    res = {"event": "hedge_target", **it}
                if res:
                    executed.append(res)
        except Exception as e:
            executed.append({"event": "strategy_error", "error": str(e)})

        # alerts on combined metrics
        metrics = {"rsi": snap["rsi"], "score": scoring["score"],
                   "adx": snap["adx"], "price": snap["price"]}
        fired = self.alerts.check(symbol, metrics)

        return {
            "symbol": symbol,
            "price": snap["price"],
            "mode": self.cfg.mode,
            "testnet": self.cfg.testnet,
            "candles": candles[-120:],
            "ema21": ind["ema21"][-120:],
            "latest": snap,
            "scoring": scoring,
            "book": book,
            "risk": self.risk.snapshot(),
            "alerts": fired,
            "executed": executed,
        }
