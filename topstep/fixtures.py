"""Deterministic synthetic intraday futures bars for offline testing.

Seeded LCG, no `random` module: a failing test fails identically twice.
Bars are 1-minute, built into real sessions (a 09:30 open, an opening drive,
a midday range, a close) so session-aware strategies like ORB exercise their
actual logic rather than a flat noise field.
"""

from __future__ import annotations

import time


class _Rand:
    def __init__(self, seed=42):
        self.s = seed

    def next(self):
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s / 0x7FFFFFFF

    def signed(self):
        return self.next() - 0.5


def session_bars(days=10, start_price=5000.0, tick=0.25, seed=42,
                 trend_per_day=0.0, vol=1.0, open_minute=570, minutes=390):
    """`days` sessions of 1-minute bars, 09:30-16:00 by default.

    Each session gets a directional opening drive then mean-reverting chop, so
    breakout and reversion strategies both have something real to find.
    """
    r = _Rand(seed)
    bars = []
    price = start_price
    # Align day 0 to a Monday 00:00 UTC so minute-of-day maths is predictable.
    base_day = (int(time.time()) // 86400 - days - 3) * 86400 * 1000

    for d in range(days):
        day_ms = base_day + d * 86400_000
        drive = (r.signed() * 2.0) * vol          # this session's character
        day_open = price
        for m in range(minutes):
            t = day_ms + (open_minute + m) * 60_000
            # Opening 45 minutes trend, then revert toward the day's open.
            if m < 45:
                bias = drive * 0.6
            else:
                bias = -0.02 * (price - day_open) + trend_per_day / minutes
            step = (bias + 1.4 * vol * r.signed()) * tick
            o = price
            c = o + step
            wick = abs(r.signed()) * 2.0 * vol * tick
            h = max(o, c) + wick
            l = min(o, c) - wick
            bars.append({
                "time": t,
                "open": round(o / tick) * tick,
                "high": round(h / tick) * tick,
                "low": round(l / tick) * tick,
                "close": round(c / tick) * tick,
                "volume": 200 + int(600 * abs(r.signed())),
            })
            price = c
    return bars


def winning_bars(days=10, **kw):
    """A gently trending tape — a strategy SHOULD make money here."""
    return session_bars(days=days, trend_per_day=30.0, **kw)


def chop_bars(days=10, **kw):
    """Directionless, wide-wicked tape — breakouts should bleed here."""
    return session_bars(days=days, trend_per_day=0.0, vol=1.6, **kw)
