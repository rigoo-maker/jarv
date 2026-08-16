"""Spread-capture market maker.

Quotes a bid below mid and an ask above mid, aiming to earn the spread when both
fill. Inventory-aware: skews quotes against current inventory so it doesn't pile
into one side. In analyze/paper mode it logs intended quotes; in live mode it
would place LIMIT post-only orders (kept conservative here).

Market making is risk-heavy in trending markets (adverse selection). Treat the
edge as small and the inventory risk as real.
"""

from __future__ import annotations

from .base import Strategy


class MarketMaker(Strategy):
    name = "market_maker"

    HALF_SPREAD_BPS = 4.0      # quote this far either side of mid
    SKEW_BPS_PER_UNIT = 2.0    # shift quotes per unit of inventory imbalance

    def evaluate(self, symbol):
        ob = self.client.book_imbalance(symbol, 10)
        mid = ob["mid"]
        intents = []
        tele = {"strategy": self.name, "symbol": symbol, **ob}
        if not mid:
            return intents, tele

        half = mid * self.HALF_SPREAD_BPS / 1e4
        # inventory skew: positive imbalance in our book => lower quotes
        skew = mid * (self.SKEW_BPS_PER_UNIT * ob["imbalance"]) / 1e4
        bid = round(mid - half - skew, 2)
        ask = round(mid + half - skew, 2)
        tele["quote_bid"], tele["quote_ask"] = bid, ask

        # Market making places resting LIMIT orders; we emit them as intents the
        # app can route. Conservative single-sided to avoid runaway in this demo.
        intents.append({"symbol": symbol, "side": "BUY", "notional_usd": None,
                        "limit_price": bid, "post_only": True,
                        "reason": f"MM bid @ {bid} (mid {mid:.2f})"})
        return intents, tele
