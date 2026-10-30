"""Candidate signals. Each returns [(date, return)] — one entry per period.

A nonzero return means capital was deployed and a round-trip cost was paid; a
zero means flat but time still elapsed. Keeping flat periods in the series is
deliberate: dropping them is how a strategy that is invested 17% of the time
gets credited with a full-time Sharpe.
"""

from __future__ import annotations

# Realistic retail round-trip costs (half-spread each way) in basis points.
# Liquid US ETFs. Override per venue — these are not commissions, which are
# zero at most retail brokers now; this is the spread you actually cross.
DEFAULT_COST_BP = {
    "SPY": 1.0, "QQQ": 1.2, "IWM": 1.6, "XLK": 1.8, "XLF": 1.8,
    "XLE": 2.0, "XLU": 2.2, "XLV": 2.0, "TLT": 1.6, "GLD": 1.4,
}


def cost(symbol, table=None):
    return (table or DEFAULT_COST_BP).get(symbol, 2.0) / 1e4


def buy_and_hold(bars, symbol=None, **_):
    """The benchmark every signal must beat. Not a strategy — the null."""
    return [(bars[i]["date"], bars[i]["close"] / bars[i - 1]["close"] - 1)
            for i in range(1, len(bars))]


def overnight(bars, symbol=None, cost_table=None, **_):
    """Hold only from close to next open. One round trip per day."""
    c = cost(symbol, cost_table)
    return [(bars[i]["date"], bars[i]["open"] / bars[i - 1]["close"] - 1 - c)
            for i in range(1, len(bars))]


def intraday(bars, symbol=None, cost_table=None, **_):
    """Hold only from open to close. The complement of `overnight`."""
    c = cost(symbol, cost_table)
    return [(bars[i]["date"], bars[i]["close"] / bars[i]["open"] - 1 - c)
            for i in range(1, len(bars))]


def overnight_trend(bars, symbol=None, lookback=200, cost_table=None, **_):
    """Overnight, but only while price is above its long moving average."""
    c = cost(symbol, cost_table)
    out = []
    for i in range(lookback, len(bars) - 1):
        sma = sum(bars[j]["close"] for j in range(i - lookback + 1, i + 1)) / lookback
        on = bars[i]["close"] > sma
        r = (bars[i + 1]["open"] / bars[i]["close"] - 1 - c) if on else 0.0
        out.append((bars[i + 1]["date"], r))
    return out


def reversal(bars, symbol=None, n_down=2, cost_table=None, **_):
    """Buy the close after n consecutive lower closes, exit the next close.

    Note what this does NOT control for: the asset's own upward drift. A
    positive mean here is mostly the equity risk premium collected on a
    subset of days, which is why `scan` always reports the increment over
    `buy_and_hold` via a Welch test rather than the raw mean.
    """
    c = cost(symbol, cost_table)
    out = []
    for i in range(n_down + 1, len(bars) - 1):
        if all(bars[j]["close"] < bars[j - 1]["close"]
               for j in range(i - n_down + 1, i + 1)):
            out.append((bars[i + 1]["date"],
                        bars[i + 1]["close"] / bars[i]["close"] - 1 - c))
    return out


def trend_following(bars, symbol=None, lookback=200, **_):
    """Long while above the moving average, flat below. Low turnover."""
    out = []
    for i in range(lookback, len(bars) - 1):
        sma = sum(bars[j]["close"] for j in range(i - lookback + 1, i + 1)) / lookback
        r = (bars[i + 1]["close"] / bars[i]["close"] - 1) if bars[i]["close"] > sma else 0.0
        out.append((bars[i + 1]["date"], r))
    return out


SIGNALS = {
    "buy&hold": buy_and_hold,
    "overnight": overnight,
    "intraday": intraday,
    "ovn+trend": overnight_trend,
    "reversal2d": lambda b, **k: reversal(b, n_down=2, **k),
    "reversal3d": lambda b, **k: reversal(b, n_down=3, **k),
    "trend200": trend_following,
}
