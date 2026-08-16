"""Strategy library for US EQUITY DAILY bars (this repo's crypto rules do not transfer).

Why a separate library at all: `strats.py` was written for 24/7 crypto minutes,
where price is roughly symmetric and shorting is native. Daily equities are a
different animal, and the differences are structural, not cosmetic:

  * **Upward drift.** Stocks go up over decades. A symmetric long/short rule
    fights that drift half the time, and shorting single names costs borrow. So
    every rule here is LONG/FLAT, and the honest benchmark is buy-and-hold, not
    zero — a strategy that returns less than B&H with the same risk has no edge,
    however green its equity curve looks.
  * **The 200-day line is the regime.** The single most durable equity fact is
    that returns above the 200-day average are better and calmer than below it.
    Most rules here are gated by it.
  * **Mean reversion lives at 2 days, not 14.** RSI(14) barely moves on daily
    bars; the equity reversion literature (Connors) uses RSI(2) < 10 with a
    trend filter. Same for Internal Bar Strength — a one-day close-near-the-low
    effect that has no analogue in the crypto set.
  * **Calendar effects are real here.** Turn-of-month and the overnight session
    carry a documented share of equity returns; crypto has no month-end pension
    flow and no overnight gap.
  * **Volatility is exogenous and observable.** This dataset ships VIX, so
    "trade when the market is calm" and "buy the panic" can be tested directly
    instead of proxied through ATR.

Same O(1) contract as `strats.py`: `precompute(candles, extras)` builds every
series once, `signal(cache, i) -> 1/0` reads bar i. Rules with an entry/exit
state machine (Connors, breakout trails, VIX spikes) precompute their position
series in the cache, so the per-bar call stays O(1) and cannot peek forward.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import indicators

TRADING_DAYS_YEAR = 252


def _sma(vals, n):
    return indicators.sma(vals, n)


def _zscore(vals, n):
    out = [None] * len(vals)
    for i in range(n, len(vals)):
        win = [v for v in vals[i - n:i] if v is not None]
        if len(win) < n // 2:
            continue
        mu = sum(win) / len(win)
        var = sum((v - mu) ** 2 for v in win) / len(win)
        sd = var ** 0.5
        if sd > 1e-12 and vals[i] is not None:
            out[i] = (vals[i] - mu) / sd
    return out


def precompute(candles, extras=None):
    """Every series the equity rules need, computed once.

    `extras` is the per-bar dict list from csvdata (VIX, rates, ...) — optional;
    the VIX rules are simply dropped from the registry when it is absent.
    """
    n = len(candles)
    close = [c["close"] for c in candles]
    high = [c["high"] for c in candles]
    low = [c["low"] for c in candles]
    open_ = [c["open"] for c in candles]

    cache = {
        "close": close, "high": high, "low": low, "open": open_,
        "sma5": _sma(close, 5), "sma20": _sma(close, 20),
        "sma50": _sma(close, 50), "sma200": _sma(close, 200),
        "rsi2": indicators.rsi(close, 2), "rsi14": indicators.rsi(close, 14),
        "atr": indicators.atr(candles, 14),
    }

    # Internal Bar Strength: where in the day's range did it close?
    cache["ibs"] = [None if h <= l else (c - l) / (h - l)
                    for c, h, l in zip(close, high, low)]

    # overnight gap (open vs prior close) — an equity-only variable
    cache["gap"] = [None] + [(open_[i] / close[i - 1] - 1) if close[i - 1] else None
                             for i in range(1, n)]

    # prior 52-week high/low, excluding today
    w = TRADING_DAYS_YEAR
    hi52 = [None] * n
    for i in range(w, n):
        hi52[i] = max(high[i - w:i])
    cache["high52"] = hi52

    # 12-1 momentum: 12-month return skipping the most recent month
    m = [None] * n
    for i in range(w + 21, n):
        past = close[i - w - 21]
        if past:
            m[i] = close[i - 21] / past - 1
    cache["mom12_1"] = m

    # calendar position: trading-day index inside its month, and days in month
    days = [datetime.fromtimestamp(c["time"] / 1000, timezone.utc) for c in candles]
    cache["dow"] = [d.weekday() for d in days]
    idx_in_month, month_len = [0] * n, [0] * n
    start = 0
    for i in range(1, n + 1):
        if i == n or (days[i].month, days[i].year) != (days[i - 1].month, days[i - 1].year):
            for j in range(start, i):
                idx_in_month[j] = j - start
                month_len[j] = i - start
            start = i
    cache["tom_idx"] = idx_in_month
    cache["month_len"] = month_len

    # VIX, if the dataset carries it
    vix = None
    if extras:
        for key in ("vix", "^vix", "vix_close"):
            if key in extras[0]:
                vix = [row.get(key) for row in extras]
                break
    cache["vix"] = vix
    cache["vix_ma50"] = _sma(vix, 50) if vix else None
    cache["vix_z"] = _zscore(vix, 20) if vix else None

    # ---- state machines, precomputed so the per-bar read stays O(1) ----
    cache["connors_state"] = _connors_state(cache)
    cache["breakout_state"] = _breakout_state(cache)
    cache["vix_spike_state"] = _vix_spike_state(cache) if vix else [0] * n

    return cache


def _connors_state(cache):
    """Connors RSI(2): buy oversold inside an uptrend, exit on the 5-day mean."""
    close, sma200, sma5, rsi2 = (cache["close"], cache["sma200"],
                                 cache["sma5"], cache["rsi2"])
    pos, out = 0, []
    for i in range(len(close)):
        if None in (sma200[i], sma5[i], rsi2[i]):
            out.append(0)
            continue
        if pos == 0:
            if close[i] > sma200[i] and rsi2[i] < 10:
                pos = 1
        else:
            if close[i] > sma5[i] or close[i] < sma200[i]:
                pos = 0
        out.append(pos)
    return out


def _breakout_state(cache):
    """52-week-high breakout, held until the 200-day line breaks."""
    close, hi52, sma200 = cache["close"], cache["high52"], cache["sma200"]
    pos, out = 0, []
    for i in range(len(close)):
        if None in (hi52[i], sma200[i]):
            out.append(0)
            continue
        if pos == 0:
            if close[i] >= hi52[i]:
                pos = 1
        elif close[i] < sma200[i]:
            pos = 0
        out.append(pos)
    return out


def _vix_spike_state(cache, hold=5):
    """Buy the panic: VIX 2-sigma above its own 20-day mean, hold a week."""
    z, close, sma200 = cache["vix_z"], cache["close"], cache["sma200"]
    pos, left, out = 0, 0, []
    for i in range(len(close)):
        if z[i] is None or sma200[i] is None:
            out.append(0)
            continue
        if pos == 0 and z[i] >= 2.0:
            pos, left = 1, hold
        elif pos == 1:
            left -= 1
            if left <= 0:
                pos = 0
        out.append(pos)
    return out


# ------------------------------------------------------------------ strategies
# Every rule returns 1 (long) or 0 (flat). No shorts: see the module docstring.

def sma200_trend(cache, i):
    """The regime rule itself: hold while price is above its 200-day average."""
    s = cache["sma200"][i]
    return 1 if s is not None and cache["close"][i] > s else 0


def golden_cross(cache, i):
    """50-day above 200-day — slower, fewer whipsaws, later exits."""
    a, b = cache["sma50"][i], cache["sma200"][i]
    return 1 if None not in (a, b) and a > b else 0


def connors_rsi2(cache, i):
    """RSI(2) < 10 above the 200-day, out at the 5-day mean (Connors)."""
    return cache["connors_state"][i]


def ibs_reversion(cache, i):
    """Close in the bottom 20% of the day's range, inside an uptrend: one-day hold."""
    ibs, s = cache["ibs"][i], cache["sma200"][i]
    if ibs is None or s is None:
        return 0
    return 1 if (ibs < 0.2 and cache["close"][i] > s) else 0


def gap_fade(cache, i):
    """Fade a >1% down-gap while the trend is intact — an equity-only setup."""
    g, s = cache["gap"][i], cache["sma200"][i]
    if g is None or s is None:
        return 0
    return 1 if (g < -0.01 and cache["close"][i] > s) else 0


def turn_of_month(cache, i):
    """Last trading day of the month through the third of the next one."""
    idx, ln = cache["tom_idx"][i], cache["month_len"][i]
    if not ln:
        return 0
    return 1 if (idx >= ln - 1 or idx <= 2) else 0


def momentum_12_1(cache, i):
    """Positive 12-month return skipping the last month (classic time-series momentum)."""
    m = cache["mom12_1"][i]
    return 1 if m is not None and m > 0 else 0


def high52_breakout(cache, i):
    """New 52-week high, held until the 200-day line gives way."""
    return cache["breakout_state"][i]


def vix_calm(cache, i):
    """Own the market only while VIX sits below its own 50-day average."""
    v, ma = cache["vix"], cache["vix_ma50"]
    if not v or v[i] is None or ma[i] is None:
        return 0
    return 1 if v[i] < ma[i] else 0


def vix_spike_reversal(cache, i):
    """Buy fear: VIX 2 sigma above its 20-day mean, hold five sessions."""
    return cache["vix_spike_state"][i]


ALL = {
    "sma200_trend": sma200_trend,
    "golden_cross": golden_cross,
    "connors_rsi2": connors_rsi2,
    "ibs_reversion": ibs_reversion,
    "gap_fade": gap_fade,
    "turn_of_month": turn_of_month,
    "momentum_12_1": momentum_12_1,
    "high52_breakout": high52_breakout,
    "vix_calm": vix_calm,
    "vix_spike_reversal": vix_spike_reversal,
}

NEEDS_VIX = ("vix_calm", "vix_spike_reversal")


def registry(cache):
    """Drop the VIX rules when the dataset has no VIX column — a strategy that is
    structurally flat is worse than absent: it would rank as 'no edge' and read
    like a finding."""
    if cache.get("vix"):
        return dict(ALL)
    return {k: v for k, v in ALL.items() if k not in NEEDS_VIX}


# ------------------------------------------------------------ parameter grids
# Every rule's own knobs, so the sweep covers the whole library instead of the
# two rules that happened to be interesting first. Each `make` returns a POSITION
# SERIES (not a signal function) so state-machine rules can be swept honestly:
# re-running a state machine at new thresholds is not the same as re-reading a
# threshold, and pretending otherwise sweeps a strategy nobody trades.


def _series(fn, n):
    return [fn(i) for i in range(n)]


def _sma_of(cache, length, _cache={}):
    key = (id(cache), length)
    if key not in _cache:
        _cache[key] = _sma(cache["close"], length)
    return _cache[key]


def _mk_sma_trend(cache, L, _y=None):
    close, ma = cache["close"], _sma_of(cache, L)
    return [1 if (ma[i] is not None and close[i] > ma[i]) else 0
            for i in range(len(close))]


def _mk_cross(cache, fast, slow):
    a, b = _sma_of(cache, fast), _sma_of(cache, slow)
    return [1 if (a[i] is not None and b[i] is not None and a[i] > b[i]) else 0
            for i in range(len(a))]


def _mk_connors(cache, entry, trend_len):
    close, rsi2, sma5 = cache["close"], cache["rsi2"], cache["sma5"]
    trend = _sma_of(cache, trend_len)
    pos, out = 0, []
    for i in range(len(close)):
        if None in (trend[i], sma5[i], rsi2[i]):
            out.append(0)
            continue
        if pos == 0:
            if close[i] > trend[i] and rsi2[i] < entry:
                pos = 1
        elif close[i] > sma5[i] or close[i] < trend[i]:
            pos = 0
        out.append(pos)
    return out


def _mk_ibs(cache, level, trend_len):
    close, ibs = cache["close"], cache["ibs"]
    trend = _sma_of(cache, trend_len)
    return [1 if (ibs[i] is not None and trend[i] is not None
                  and ibs[i] < level and close[i] > trend[i]) else 0
            for i in range(len(close))]


def _mk_gap(cache, gap_pct, trend_len):
    close, gap = cache["close"], cache["gap"]
    trend = _sma_of(cache, trend_len)
    return [1 if (gap[i] is not None and trend[i] is not None
                  and gap[i] < -gap_pct / 100.0 and close[i] > trend[i]) else 0
            for i in range(len(close))]


def _mk_tom(cache, before, after):
    idx, ln = cache["tom_idx"], cache["month_len"]
    return [1 if (ln[i] and (idx[i] >= ln[i] - before or idx[i] < after)) else 0
            for i in range(len(idx))]


def _mk_mom(cache, lookback, skip):
    close = cache["close"]
    out = [0] * len(close)
    for i in range(lookback + skip, len(close)):
        past = close[i - lookback - skip]
        if past and close[i - skip] / past - 1 > 0:
            out[i] = 1
    return out


def _mk_breakout(cache, lookback, exit_len):
    close, high = cache["close"], cache["high"]
    exit_ma = _sma_of(cache, exit_len)
    pos, out = 0, []
    for i in range(len(close)):
        if i < lookback or exit_ma[i] is None:
            out.append(0)
            continue
        if pos == 0:
            if close[i] >= max(high[i - lookback:i]):
                pos = 1
        elif close[i] < exit_ma[i]:
            pos = 0
        out.append(pos)
    return out


def _mk_vix_calm(cache, ma_len, _y=None):
    v = cache["vix"]
    if not v:
        return [0] * len(cache["close"])
    ma = _sma(v, ma_len)
    return [1 if (v[i] is not None and ma[i] is not None and v[i] < ma[i]) else 0
            for i in range(len(v))]


def _mk_vix_spike(cache, z_thresh, hold):
    z, close, sma200 = cache["vix_z"], cache["close"], cache["sma200"]
    if not z:
        return [0] * len(close)
    pos, left, out = 0, 0, []
    for i in range(len(close)):
        if z[i] is None or sma200[i] is None:
            out.append(0)
            continue
        if pos == 0 and z[i] >= z_thresh:
            pos, left = 1, hold
        elif pos == 1:
            left -= 1
            if left <= 0:
                pos = 0
        out.append(pos)
    return out


SWEEP_SPECS = {
    "sma200_trend": {"x": ("trend SMA", [20, 50, 100, 150, 200, 250, 300]),
                     "y": None, "make": _mk_sma_trend},
    "golden_cross": {"x": ("slow SMA", [100, 150, 200, 250, 300]),
                     "y": ("fast SMA", [10, 20, 50, 75, 100]),
                     "make": lambda c, x, y: _mk_cross(c, y, x)},
    "connors_rsi2": {"x": ("trend SMA", [50, 100, 150, 200, 250, 300]),
                     "y": ("RSI2 entry", [5, 10, 15, 20, 25, 30]),
                     "make": lambda c, x, y: _mk_connors(c, y, x)},
    "ibs_reversion": {"x": ("trend SMA", [50, 100, 200, 300]),
                      "y": ("IBS below", [0.05, 0.1, 0.15, 0.2, 0.3, 0.4]),
                      "make": lambda c, x, y: _mk_ibs(c, y, x)},
    "gap_fade": {"x": ("trend SMA", [50, 100, 200, 300]),
                 "y": ("down-gap %", [0.5, 0.75, 1.0, 1.5, 2.0, 3.0]),
                 "make": lambda c, x, y: _mk_gap(c, y, x)},
    "turn_of_month": {"x": ("days into month", [1, 2, 3, 4, 5]),
                      "y": ("days before end", [1, 2, 3, 4, 5]),
                      "make": lambda c, x, y: _mk_tom(c, y, x)},
    "momentum_12_1": {"x": ("lookback bars", [63, 126, 189, 252, 378, 504]),
                      "y": ("skip bars", [0, 5, 10, 21, 42]),
                      "make": lambda c, x, y: _mk_mom(c, x, y)},
    "high52_breakout": {"x": ("high lookback", [63, 126, 189, 252, 378]),
                        "y": ("exit SMA", [50, 100, 150, 200, 250]),
                        "make": lambda c, x, y: _mk_breakout(c, x, y)},
    "vix_calm": {"x": ("VIX SMA", [10, 20, 50, 100, 200]), "y": None,
                 "make": _mk_vix_calm},
    "vix_spike_reversal": {"x": ("VIX z-score", [1.0, 1.5, 2.0, 2.5, 3.0]),
                           "y": ("hold bars", [1, 3, 5, 10, 21]),
                           "make": _mk_vix_spike},
}


def sweep_specs(cache):
    """Grids for the rules this dataset can actually support."""
    if cache.get("vix"):
        return dict(SWEEP_SPECS)
    return {k: v for k, v in SWEEP_SPECS.items() if k not in NEEDS_VIX}
