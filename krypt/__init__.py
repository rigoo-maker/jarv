"""KRYPT — advanced crypto trading toolkit.

Live/paper/analyze trading for Binance with advanced technical analysis,
a weighted signal-scoring engine, hedging, and HFT-style scalping.

SAFETY: default mode is read-only `analyze`. Live trading requires two locks
(--mode live AND env KRYPT_ALLOW_LIVE=1) and defaults to Binance TESTNET.
Nothing here is financial advice. Trading crypto can lose all your money.
"""

__version__ = "0.1.0"
