"""Trading strategies for KRYPT."""

from .scalper import ScalperHFT
from .market_maker import MarketMaker
from .hedge import DeltaHedge
from .trend import TrendFollower

REGISTRY = {
    "scalper": ScalperHFT,
    "market_maker": MarketMaker,
    "hedge": DeltaHedge,
    "trend": TrendFollower,
}


def make_strategy(name, cfg, client, scorer=None):
    cls = REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"unknown strategy '{name}'. choices: {list(REGISTRY)}")
    return cls(cfg, client)
