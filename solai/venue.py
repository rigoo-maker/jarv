"""Execution venues.

`PaperVenue` simulates fills against observed prices with explicit costs.
`JupiterVenue` is the live path and is deliberately NOT implemented.

The live stub is not laziness — it is the design. Signing a Solana swap means
a hot wallet private key sitting in the environment of a scanner that runs
unattended and calls an LLM. Before that is worth building, the paper record
has to show an edge that survives its own costs. The interface below is the
whole contract, so wiring a real venue later is a small, reviewable change.
"""

from __future__ import annotations

from .config import USDC


class Venue:
    name = "abstract"

    def buy(self, mint, usd_size, price, **kw):
        raise NotImplementedError

    def sell(self, mint, qty, price, **kw):
        raise NotImplementedError


class PaperVenue(Venue):
    """Simulated fills. Costs are charged on BOTH legs, always.

    The single most common way a paper record lies is by filling at the mid
    price with no fee. This one fills through the spread and pays the fee, so
    a strategy that only works at zero cost shows up as a loser here — which
    is the entire reason to paper trade.
    """
    name = "paper"

    def __init__(self, fee_bps=30.0, slippage_bps=50.0):
        self.fee_bps, self.slippage_bps = fee_bps, slippage_bps

    def _cost(self):
        return (self.fee_bps + self.slippage_bps) / 10_000.0

    def buy(self, mint, usd_size, price, **kw):
        c = self._cost()
        fill_price = price * (1 + c)          # you pay up
        qty = usd_size / fill_price
        return {"side": "buy", "mint": mint, "qty": qty,
                "price": fill_price, "ref_price": price,
                "usd": usd_size, "cost_pct": c * 100, "venue": self.name}

    def sell(self, mint, qty, price, **kw):
        c = self._cost()
        fill_price = price * (1 - c)          # you get hit down
        usd = qty * fill_price
        return {"side": "sell", "mint": mint, "qty": qty,
                "price": fill_price, "ref_price": price,
                "usd": usd, "cost_pct": c * 100, "venue": self.name}


class JupiterVenue(Venue):
    """Live Solana execution via Jupiter swap. NOT IMPLEMENTED BY DESIGN.

    To implement, you would need to:
      1. Load a keypair from env (never from disk, never committed).
      2. GET  /swap/v1/quote   for the exact route.
      3. POST /swap/v1/swap    with userPublicKey to get a serialized tx.
      4. Sign it locally and send via sendTransaction, then confirm.
      5. Re-quote and abort if the route moved beyond your slippage budget
         between quote and send — on Solana this happens constantly.

    Each of those steps can lose real money in a way paper trading cannot.
    Do not enable this until the paper record justifies it.
    """
    name = "jupiter"

    def __init__(self, cfg):
        self.cfg = cfg

    def _refuse(self):
        raise NotImplementedError(
            "Live Jupiter execution is intentionally not implemented. "
            "SOLAI ships paper-only: prove an edge in `paper` mode first, then "
            "implement JupiterVenue.buy/sell deliberately and review it. "
            "See solai/venue.py for the required steps."
        )

    def buy(self, mint, usd_size, price, **kw):
        self._refuse()

    def sell(self, mint, qty, price, **kw):
        self._refuse()


def build(cfg):
    """Pick a venue for the configured mode, enforcing the locks."""
    if cfg.mode == "live":
        if not cfg.allow_live:
            raise PermissionError(
                "mode=live requires the second lock: set SOLAI_ALLOW_LIVE=1. "
                "(Even then, live execution is not implemented — see venue.py.)")
        return JupiterVenue(cfg)
    return PaperVenue(fee_bps=cfg.risk.fee_bps,
                      slippage_bps=cfg.risk.slippage_bps)
