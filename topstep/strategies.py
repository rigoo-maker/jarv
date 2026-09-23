"""Intraday futures strategies.

Every strategy returns a Signal carrying an explicit STOP. That is not
stylistic: on a trailing-drawdown account, position size is derived from stop
distance, so a strategy without a defined stop cannot be sized and is therefore
not tradeable here. A signal with no stop is rejected rather than defaulted.

Candles use krypt's format -- {"time","open","high","low","close","volume"} --
so krypt.indicators works on them unchanged.

None of these are edges. They are hypotheses with explicit, testable rules.
The backtester's job is to tell you which (if any) survive costs and the
account rules; the prior should be that most do not.
"""

from __future__ import annotations

from dataclasses import dataclass

from krypt import indicators


@dataclass
class Signal:
    side: str                 # "long" | "short"
    entry: float
    stop: float
    target: float = None
    reason: str = ""

    @property
    def stop_distance(self) -> float:
        return abs(self.entry - self.stop)

    def stop_ticks(self, instrument) -> int:
        return max(1, round(instrument.ticks(self.stop_distance)))

    def rr(self) -> float:
        if self.target is None or self.stop_distance == 0:
            return 0.0
        return abs(self.target - self.entry) / self.stop_distance


def _closes(bars):
    return [b["close"] for b in bars]


def _last(series):
    for v in reversed(series):
        if v is not None:
            return v
    return None


def _minute_of_day(bar, tz_offset_min=0):
    """Minutes since midnight in the exchange's display timezone."""
    ms = bar["time"]
    return int(((ms // 60000) + tz_offset_min) % 1440)


# ---------------------------------------------------------------------------
# 1. Opening range breakout
# ---------------------------------------------------------------------------
def opening_range_breakout(bars, i, *, or_minutes=30, session_open_min=570,
                           atr_mult=1.0, tz_offset_min=0, **_):
    """Break of the first N minutes of the session, stopped at the far side.

    The oldest intraday futures pattern there is, and heavily mined — treat a
    good backtest here with suspicion until it survives out-of-sample.
    """
    if i < 5:
        return None
    today = [b for b in bars[:i + 1]
             if _minute_of_day(b, tz_offset_min) >= session_open_min]
    if len(today) < 3:
        return None
    or_bars = [b for b in today
               if _minute_of_day(b, tz_offset_min) < session_open_min + or_minutes]
    if len(or_bars) < 2:
        return None
    cur = bars[i]
    if _minute_of_day(cur, tz_offset_min) < session_open_min + or_minutes:
        return None                      # still forming the range

    hi = max(b["high"] for b in or_bars)
    lo = min(b["low"] for b in or_bars)
    rng = hi - lo
    if rng <= 0:
        return None
    # Only take the first break: if price already traded outside, stand down.
    after = [b for b in today
             if _minute_of_day(b, tz_offset_min) >= session_open_min + or_minutes]
    prior = after[:-1]
    if any(b["close"] > hi or b["close"] < lo for b in prior):
        return None

    atr = _last(indicators.atr(bars[:i + 1], 14)) or rng
    if cur["close"] > hi:
        return Signal("long", cur["close"], lo,
                      cur["close"] + atr_mult * rng,
                      f"ORB up: close {cur['close']:.2f} > OR high {hi:.2f}")
    if cur["close"] < lo:
        return Signal("short", cur["close"], hi,
                      cur["close"] - atr_mult * rng,
                      f"ORB down: close {cur['close']:.2f} < OR low {lo:.2f}")
    return None


# ---------------------------------------------------------------------------
# 2. VWAP reversion
# ---------------------------------------------------------------------------
def vwap_reversion(bars, i, *, band_atr=1.5, atr_period=14, **_):
    """Fade an extension away from session VWAP, target VWAP itself.

    Works in balanced/rotational sessions and gets run over in trend days,
    which is precisely the risk the trailing drawdown punishes hardest.
    """
    if i < atr_period + 5:
        return None
    window = bars[:i + 1]
    vw = _last(indicators.vwap(window))
    atr = _last(indicators.atr(window, atr_period))
    if not vw or not atr or atr <= 0:
        return None
    cur = bars[i]
    dist = cur["close"] - vw
    if dist > band_atr * atr:
        return Signal("short", cur["close"], cur["close"] + atr, vw,
                      f"{dist/atr:.1f} ATR above VWAP {vw:.2f}")
    if dist < -band_atr * atr:
        return Signal("long", cur["close"], cur["close"] - atr, vw,
                      f"{abs(dist)/atr:.1f} ATR below VWAP {vw:.2f}")
    return None


# ---------------------------------------------------------------------------
# 3. Trend pullback
# ---------------------------------------------------------------------------
def trend_pullback(bars, i, *, fast=9, slow=21, trend=50, atr_period=14,
                   rr=2.0, **_):
    """Trade with the EMA stack, entering on a pullback to the fast EMA."""
    if i < trend + 5:
        return None
    window = bars[:i + 1]
    cl = _closes(window)
    e_f = _last(indicators.ema(cl, fast))
    e_s = _last(indicators.ema(cl, slow))
    e_t = _last(indicators.ema(cl, trend))
    atr = _last(indicators.atr(window, atr_period))
    if None in (e_f, e_s, e_t, atr) or atr <= 0:
        return None
    cur, prev = bars[i], bars[i - 1]

    up = e_f > e_s > e_t
    down = e_f < e_s < e_t
    if up and prev["low"] <= e_f and cur["close"] > prev["close"]:
        stop = cur["close"] - 1.2 * atr
        return Signal("long", cur["close"], stop,
                      cur["close"] + rr * (cur["close"] - stop),
                      "uptrend pullback to EMA9 reclaimed")
    if down and prev["high"] >= e_f and cur["close"] < prev["close"]:
        stop = cur["close"] + 1.2 * atr
        return Signal("short", cur["close"], stop,
                      cur["close"] - rr * (stop - cur["close"]),
                      "downtrend pullback to EMA9 rejected")
    return None


# ---------------------------------------------------------------------------
# 4. Range fade
# ---------------------------------------------------------------------------
def range_fade(bars, i, *, lookback=40, atr_period=14, min_touches=2,
               max_range_atr=6.0, **_):
    """Fade the edge of an established range while it stays a range."""
    if i < lookback + atr_period:
        return None
    window = bars[:i + 1]
    seg = window[-lookback:]
    hi = max(b["high"] for b in seg[:-1])
    lo = min(b["low"] for b in seg[:-1])
    atr = _last(indicators.atr(window, atr_period))
    if not atr or atr <= 0:
        return None
    rng = hi - lo
    if rng <= 0 or rng > max_range_atr * atr:
        return None                      # too wide to be a range
    tol = 0.25 * atr
    touches_hi = sum(1 for b in seg[:-1] if b["high"] >= hi - tol)
    touches_lo = sum(1 for b in seg[:-1] if b["low"] <= lo + tol)
    cur = bars[i]
    if cur["high"] >= hi - tol and touches_hi >= min_touches:
        return Signal("short", cur["close"], hi + 0.75 * atr, lo + 0.25 * rng,
                      f"fade range high {hi:.2f} ({touches_hi} touches)")
    if cur["low"] <= lo + tol and touches_lo >= min_touches:
        return Signal("long", cur["close"], lo - 0.75 * atr, hi - 0.25 * rng,
                      f"fade range low {lo:.2f} ({touches_lo} touches)")
    return None


# ---------------------------------------------------------------------------
# 5. Momentum continuation
# ---------------------------------------------------------------------------
def momentum_continuation(bars, i, *, atr_period=14, impulse_atr=1.5,
                          consolidation=3, rr=1.5, **_):
    """An impulse bar, then a shallow pause, then continuation."""
    if i < atr_period + consolidation + 2:
        return None
    window = bars[:i + 1]
    atr = _last(indicators.atr(window, atr_period))
    if not atr or atr <= 0:
        return None
    imp = bars[i - consolidation - 1]
    body = imp["close"] - imp["open"]
    if abs(body) < impulse_atr * atr:
        return None
    pause = bars[i - consolidation:i]
    if not pause:
        return None
    ph = max(b["high"] for b in pause)
    pl = min(b["low"] for b in pause)
    if (ph - pl) > 1.2 * atr:
        return None                      # pause too deep; impulse is spent
    cur = bars[i]
    if body > 0 and cur["close"] > ph:
        stop = pl - 0.25 * atr
        return Signal("long", cur["close"], stop,
                      cur["close"] + rr * (cur["close"] - stop),
                      "bull impulse + shallow pause, breakout")
    if body < 0 and cur["close"] < pl:
        stop = ph + 0.25 * atr
        return Signal("short", cur["close"], stop,
                      cur["close"] - rr * (stop - cur["close"]),
                      "bear impulse + shallow pause, breakdown")
    return None


STRATEGIES = {
    "orb": opening_range_breakout,
    "vwap_reversion": vwap_reversion,
    "trend_pullback": trend_pullback,
    "range_fade": range_fade,
    "momentum": momentum_continuation,
}
