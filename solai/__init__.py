"""SOLAI — an AI-assisted Solana signal trader.

Pipeline: discover candidates -> gather signals (DEX microstructure, on-chain
safety, smart money, TA) -> hard safety filter -> deterministic score ->
optional Claude analyst verdict -> paper execution with a persistent record.

Core is pure stdlib. The Claude analyst layer needs `pip install anthropic`;
without it the engine runs scorer-only and says so.
"""

__version__ = "0.1.0"
