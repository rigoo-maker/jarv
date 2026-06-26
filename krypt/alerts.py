"""Threshold alerts.

Fire when a metric crosses a configured threshold. Alerts are de-duplicated so a
condition that stays true doesn't spam every tick (edge-triggered).
"""

from __future__ import annotations


class AlertEngine:
    def __init__(self, rules=None):
        # rule: {"symbol","metric","op",">"|"<","value","msg"}
        self.rules = rules or []
        self._fired = set()

    def add(self, symbol, metric, op, value, msg=None):
        self.rules.append({"symbol": symbol, "metric": metric, "op": op,
                           "value": value, "msg": msg})

    def check(self, symbol, metrics: dict):
        out = []
        for r in self.rules:
            if r["symbol"] not in (symbol, "*"):
                continue
            val = metrics.get(r["metric"])
            if val is None:
                continue
            hit = val > r["value"] if r["op"] == ">" else val < r["value"]
            key = (symbol, r["metric"], r["op"], r["value"])
            if hit and key not in self._fired:
                self._fired.add(key)
                out.append({
                    "symbol": symbol, "metric": r["metric"],
                    "value": round(val, 4), "threshold": r["value"],
                    "msg": r["msg"] or f"{symbol} {r['metric']} {r['op']} {r['value']}",
                })
            elif not hit:
                self._fired.discard(key)
        return out


def default_rules(symbol):
    return AlertEngine([
        {"symbol": symbol, "metric": "rsi", "op": ">", "value": 75,
         "msg": f"{symbol} RSI overbought (>75)"},
        {"symbol": symbol, "metric": "rsi", "op": "<", "value": 25,
         "msg": f"{symbol} RSI oversold (<25)"},
        {"symbol": symbol, "metric": "score", "op": ">", "value": 60,
         "msg": f"{symbol} strong BUY confluence (score>60)"},
        {"symbol": symbol, "metric": "score", "op": "<", "value": -60,
         "msg": f"{symbol} strong SELL confluence (score<-60)"},
    ])
