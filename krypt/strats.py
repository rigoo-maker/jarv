"""Library of 10 backtestable strategies (TA + HF flavored).

For speed, indicator series are computed ONCE via `precompute(candles)` into a
cache, and each strategy reads the cache at bar `i` in O(1):

    signal(cache, i) -> int   # +1 long, 0 flat, -1 short  (desired NEXT position)

This makes a full backtest O(n) instead of O(n^2) — essential for 3 weeks of
1-minute data (~30k candles). Use `signal_now(name, candles)` for live use.

They are deliberately simple and distinct so the backtester can rank them and
show which (if any) actually have edge after costs.
"""

from __future__ import annotations

from . import indicators
from .scoring import score_snapshot

DONCHIAN_WIN = 20


def precompute(candles):
    """Compute every indicator series once. Returns a cache dict of equal-length
    lists aligned to `candles`."""
    close = [c["close"] for c in candles]
    high = [c["high"] for c in candles]
    low = [c["low"] for c in candles]
    n = len(candles)

    m = indicators.macd(close)
    bb = indicators.bollinger(close)
    sr = indicators.stoch_rsi(close)
    ad = indicators.adx(candles)

    cache = {
        "close": close, "high": high, "low": low,
        "ema9": indicators.ema(close, 9),
        "ema21": indicators.ema(close, 21),
        "ema50": indicators.ema(close, 50),
        "macd_line": m["macd"], "macd_signal": m["signal"],
        "rsi": indicators.rsi(close, 14),
        "bb_upper": bb["upper"], "bb_lower": bb["lower"], "bb_mid": bb["mid"],
        "vwap": indicators.vwap(candles),
        "stoch_k": sr["k"], "stoch_d": sr["d"],
        "adx": ad["adx"], "plus_di": ad["plus_di"], "minus_di": ad["minus_di"],
    }

    # rolling Donchian channel (prior-N high/low, excluding current bar)
    dhi = [None] * n
    dlo = [None] * n
    for i in range(DONCHIAN_WIN, n):
        dhi[i] = max(high[i - DONCHIAN_WIN:i])
        dlo[i] = min(low[i - DONCHIAN_WIN:i])
    cache["don_hi"], cache["don_lo"] = dhi, dlo

    # confluence score per bar — cheap now that all series exist
    score = [None] * n
    for i in range(n):
        latest = {
            "price": close[i], "ema9": cache["ema9"][i], "ema21": cache["ema21"][i],
            "ema50": cache["ema50"][i], "rsi": cache["rsi"][i],
            "macd": cache["macd_line"][i], "macd_signal": cache["macd_signal"][i],
            "macd_hist": (None if cache["macd_line"][i] is None or cache["macd_signal"][i] is None
                          else cache["macd_line"][i] - cache["macd_signal"][i]),
            "bb_upper": cache["bb_upper"][i], "bb_mid": cache["bb_mid"][i],
            "bb_lower": cache["bb_lower"][i], "vwap": cache["vwap"][i],
            "stoch_k": cache["stoch_k"][i], "stoch_d": cache["stoch_d"][i],
            "adx": cache["adx"][i], "plus_di": cache["plus_di"][i],
            "minus_di": cache["minus_di"][i],
        }
        score[i] = score_snapshot(latest)["score"]
    cache["score"] = score
    return cache


# 1. EMA crossover (trend)
def ema_cross(cache, i):
    e9, e21 = cache["ema9"][i], cache["ema21"][i]
    if e9 is None or e21 is None:
        return 0
    return 1 if e9 > e21 else -1


# 2. MACD signal cross (momentum)
def macd_cross(cache, i):
    line, sig = cache["macd_line"][i], cache["macd_signal"][i]
    if line is None or sig is None:
        return 0
    return 1 if line > sig else -1


# 3. RSI mean reversion (fade extremes)
def rsi_reversion(cache, i):
    r = cache["rsi"][i]
    if r is None:
        return 0
    if r <= 30:
        return 1
    if r >= 70:
        return -1
    return 0


# 4. Bollinger breakout (volatility expansion)
def bb_breakout(cache, i):
    u, l, px = cache["bb_upper"][i], cache["bb_lower"][i], cache["close"][i]
    if u is None:
        return 0
    if px > u:
        return 1
    if px < l:
        return -1
    return 0


# 5. Bollinger mean reversion (fade bands)
def bb_reversion(cache, i):
    u, l, px = cache["bb_upper"][i], cache["bb_lower"][i], cache["close"][i]
    if u is None:
        return 0
    if px <= l:
        return 1
    if px >= u:
        return -1
    return 0


# 6. Donchian channel breakout (trend / HF)
def donchian_breakout(cache, i):
    hi, lo, px = cache["don_hi"][i], cache["don_lo"][i], cache["close"][i]
    if hi is None:
        return 0
    if px >= hi:
        return 1
    if px <= lo:
        return -1
    return 0


# 7. VWAP reversion (intraday HF)
def vwap_reversion(cache, i, k=0.004):
    v, px = cache["vwap"][i], cache["close"][i]
    if not v:
        return 0
    if px < v * (1 - k):
        return 1
    if px > v * (1 + k):
        return -1
    return 0


# 8. Stochastic-RSI cross (fast momentum / HF)
def stochrsi_cross(cache, i):
    k, d = cache["stoch_k"][i], cache["stoch_d"][i]
    if k is None or d is None:
        return 0
    if k > d and k < 80:
        return 1
    if k < d and k > 20:
        return -1
    return 0


# 9. ADX trend filter + DI cross (only strong trends)
def adx_di(cache, i):
    adx, pdi, mdi = cache["adx"][i], cache["plus_di"][i], cache["minus_di"][i]
    if adx is None or pdi is None:
        return 0
    if adx < 25:
        return 0
    return 1 if pdi > mdi else -1


# 10. Confluence ensemble (weighted scoring engine)
def confluence(cache, i, enter=35):
    s = cache["score"][i]
    if s is None:
        return 0
    if s >= enter:
        return 1
    if s <= -enter:
        return -1
    return 0


REGISTRY = {
    "ema_cross": ema_cross,
    "macd_cross": macd_cross,
    "rsi_reversion": rsi_reversion,
    "bb_breakout": bb_breakout,
    "bb_reversion": bb_reversion,
    "donchian_breakout": donchian_breakout,
    "vwap_reversion": vwap_reversion,
    "stochrsi_cross": stochrsi_cross,
    "adx_di": adx_di,
    "confluence": confluence,
}


def signal_now(name, candles):
    """Convenience for live use: compute the current signal for one strategy."""
    cache = precompute(candles)
    return REGISTRY[name](cache, len(candles) - 1)
