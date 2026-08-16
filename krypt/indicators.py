"""Advanced technical-analysis indicators — pure Python, no dependencies.

Every function takes plain lists and returns lists aligned to the input length
(leading values are None until the indicator has enough data). A candle is a
dict: {time, open, high, low, close, volume}.

Indicators: SMA, EMA, RSI, MACD, Bollinger Bands, ATR, VWAP, Stochastic RSI,
ADX, OBV. These are standard tools, not a guarantee of profit.
"""

from __future__ import annotations

import math


def sma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def ema(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gain += max(ch, 0.0)
        loss += max(-ch, 0.0)
    ag, al = gain / period, loss / period
    out[period] = 100 - 100 / (1 + (ag / al if al else 1e9))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0.0)) / period
        al = (al * (period - 1) + max(-ch, 0.0)) / period
        rs = ag / al if al else 1e9
        out[i] = 100 - 100 / (1 + rs)
    return out


def macd(closes, fast=12, slow=26, signal=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [(ef[i] - es[i]) if (ef[i] is not None and es[i] is not None) else None
            for i in range(len(closes))]
    vals = [x for x in line if x is not None]
    sig_tail = ema(vals, signal)
    sig = [None] * len(closes)
    j = 0
    for i in range(len(closes)):
        if line[i] is not None:
            sig[i] = sig_tail[j]
            j += 1
    hist = [(line[i] - sig[i]) if (line[i] is not None and sig[i] is not None) else None
            for i in range(len(closes))]
    return {"macd": line, "signal": sig, "hist": hist}


def bollinger(closes, period=20, mult=2.0):
    mid = sma(closes, period)
    upper = [None] * len(closes)
    lower = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1:i + 1]
        m = mid[i]
        var = sum((c - m) ** 2 for c in window) / period
        sd = math.sqrt(var)
        upper[i] = m + mult * sd
        lower[i] = m - mult * sd
    return {"mid": mid, "upper": upper, "lower": lower}


def true_range(candles):
    tr = [None] * len(candles)
    for i in range(1, len(candles)):
        h, l = candles[i]["high"], candles[i]["low"]
        pc = candles[i - 1]["close"]
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    return tr


def atr(candles, period=14):
    tr = true_range(candles)
    out = [None] * len(candles)
    vals = [x for x in tr if x is not None]
    if len(vals) < period:
        return out
    first = sum(tr[1:period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, len(candles)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def vwap(candles):
    """Cumulative VWAP over the provided window."""
    out = [None] * len(candles)
    cum_pv = cum_v = 0.0
    for i, c in enumerate(candles):
        typical = (c["high"] + c["low"] + c["close"]) / 3
        cum_pv += typical * c["volume"]
        cum_v += c["volume"]
        out[i] = cum_pv / cum_v if cum_v else None
    return out


def stoch_rsi(closes, period=14, k_smooth=3, d_smooth=3):
    r = rsi(closes, period)
    vals = [x for x in r if x is not None]
    raw = [None] * len(closes)
    offset = len(closes) - len(vals)
    for idx in range(period, len(vals)):
        window = vals[idx - period:idx + 1]
        lo, hi = min(window), max(window)
        raw[offset + idx] = (vals[idx] - lo) / (hi - lo) * 100 if hi > lo else 0.0
    k = sma([x if x is not None else 0 for x in raw], k_smooth)
    d = sma([x if x is not None else 0 for x in k], d_smooth)
    return {"k": k, "d": d}


def adx(candles, period=14):
    """Average Directional Index — trend strength (0-100)."""
    n = len(candles)
    out = {"adx": [None] * n, "plus_di": [None] * n, "minus_di": [None] * n}
    if n < 2 * period:
        return out
    tr = true_range(candles)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = candles[i]["high"] - candles[i - 1]["high"]
        dn = candles[i - 1]["low"] - candles[i]["low"]
        plus_dm[i] = up if (up > dn and up > 0) else 0.0
        minus_dm[i] = dn if (dn > up and dn > 0) else 0.0
    atr_s = sum(tr[1:period + 1])
    pdm_s = sum(plus_dm[1:period + 1])
    mdm_s = sum(minus_dm[1:period + 1])
    dx_list = []
    for i in range(period + 1, n):
        atr_s = atr_s - atr_s / period + tr[i]
        pdm_s = pdm_s - pdm_s / period + plus_dm[i]
        mdm_s = mdm_s - mdm_s / period + minus_dm[i]
        pdi = 100 * pdm_s / atr_s if atr_s else 0.0
        mdi = 100 * mdm_s / atr_s if atr_s else 0.0
        out["plus_di"][i] = pdi
        out["minus_di"][i] = mdi
        denom = pdi + mdi
        dx = 100 * abs(pdi - mdi) / denom if denom else 0.0
        dx_list.append(dx)
        if len(dx_list) == period:
            out["adx"][i] = sum(dx_list) / period
        elif len(dx_list) > period:
            out["adx"][i] = (out["adx"][i - 1] * (period - 1) + dx) / period
    return out


def obv(candles):
    out = [0.0] * len(candles)
    for i in range(1, len(candles)):
        if candles[i]["close"] > candles[i - 1]["close"]:
            out[i] = out[i - 1] + candles[i]["volume"]
        elif candles[i]["close"] < candles[i - 1]["close"]:
            out[i] = out[i - 1] - candles[i]["volume"]
        else:
            out[i] = out[i - 1]
    return out


def compute_all(candles):
    """Run the full indicator suite; return latest snapshot + full series."""
    closes = [c["close"] for c in candles]
    m = macd(closes)
    bb = bollinger(closes)
    sr = stoch_rsi(closes)
    adx_d = adx(candles)

    def last(x):
        for v in reversed(x):
            if v is not None:
                return v
        return None

    return {
        "closes": closes,
        "ema9": ema(closes, 9),
        "ema21": ema(closes, 21),
        "ema50": ema(closes, 50),
        "rsi": rsi(closes, 14),
        "macd": m,
        "bollinger": bb,
        "atr": atr(candles, 14),
        "vwap": vwap(candles),
        "stoch_rsi": sr,
        "adx": adx_d,
        "obv": obv(candles),
        "latest": {
            "price": closes[-1] if closes else None,
            "ema9": last(ema(closes, 9)),
            "ema21": last(ema(closes, 21)),
            "ema50": last(ema(closes, 50)),
            "rsi": last(rsi(closes, 14)),
            "macd": last(m["macd"]),
            "macd_signal": last(m["signal"]),
            "macd_hist": last(m["hist"]),
            "bb_upper": last(bb["upper"]),
            "bb_mid": last(bb["mid"]),
            "bb_lower": last(bb["lower"]),
            "atr": last(atr(candles, 14)),
            "vwap": last(vwap(candles)),
            "stoch_k": last(sr["k"]),
            "stoch_d": last(sr["d"]),
            "adx": last(adx_d["adx"]),
            "plus_di": last(adx_d["plus_di"]),
            "minus_di": last(adx_d["minus_di"]),
        },
    }
