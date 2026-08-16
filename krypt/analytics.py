"""Edge analytics: the matrices behind the heatmaps.

`compare` answers "which strategy won over the whole sample?" — which is the
wrong question, because a strategy can post a great total number and be *dead
for the last week*. This module answers the question you actually care about:

    which strategies still have an ACTIVE edge, right now, after costs?

It does that by slicing the sample instead of averaging it away:

  * `window_matrix`  strategy x time-slice   -> is the edge alive or decaying?
  * `regime_matrix`  strategy x (trend, vol) -> WHERE the edge lives
                                                (mean reversion pays in chop,
                                                 breakout pays in strong trend)
  * `hour_matrix`    strategy x hour (UTC)   -> session effects
  * `cost_matrix`    strategy x fee+slip bps -> is it an edge or a cost illusion?
  * `corr_matrix`    strategy x strategy     -> which "different" strategies are
                                                the same trade in a wig
  * `overlap_matrix` strategy x strategy     -> position-level co-exposure
  * `sweep_*`        parameter grids         -> a real edge is a PLATEAU,
                                                a fitted one is a lone spike

and then folds those into an `active_edge` score with an out-of-sample split.

Conventions (one, consistently, everywhere in this module):
  * Sharpe is ANNUALIZED from per-bar net returns (crypto = 24/7/365).
    NOTE: `backtest.stats()` reports a different, per-sample Sharpe — the two
    numbers are not comparable, which is why this module recomputes its own.
  * Position at bar i is the signal at bar i, so the return it earns lands on
    bar i+1 — same next-bar convention as the backtester (no look-ahead).
  * Every return is NET: fees + slippage are charged on position changes.

Everything is stdlib and O(n) per strategy over a precomputed indicator cache.
"""

from __future__ import annotations

import math

import time

from . import backtest as bt
from . import indicators
from . import strats as stratlib

# ---------------------------------------------------------------- time helpers

INTERVAL_SECS = {
    "1s": 1, "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600, "8h": 28800,
    "12h": 43200, "1d": 86400,
}

YEAR_SECS = 365 * 24 * 3600


def bars_per_year(interval: str) -> float:
    """Fallback only — assumes a 24/7 calendar. Prefer `empirical_bars_per_year`."""
    return YEAR_SECS / INTERVAL_SECS.get(interval, 60)


def empirical_bars_per_year(candles, interval="1m"):
    """Annualization factor measured from the timestamps.

    This matters more than it looks. A 24/7 assumption on US equity daily bars
    counts 365 bars a year instead of ~252, inflating every Sharpe by ~20%; on
    intraday equity bars (6.5h sessions) the error is nearly 2x. Counting the
    bars the data actually contains handles weekends, holidays, half-days and
    exchange hours without a calendar library.
    """
    if len(candles) < 3:
        return bars_per_year(interval)
    span = (candles[-1]["time"] - candles[0]["time"]) / 1000.0
    if span <= 0:
        return bars_per_year(interval)
    return (len(candles) - 1) / (span / YEAR_SECS)


def median_bar_secs(candles):
    """Median spacing — the mean is wrecked by weekend gaps."""
    if len(candles) < 3:
        return 60.0
    gaps = sorted(candles[i]["time"] - candles[i - 1]["time"]
                  for i in range(1, len(candles)))
    return gaps[len(gaps) // 2] / 1000.0


# ------------------------------------------------------------------ math utils

def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    mu = sum(xs) / len(xs)
    return math.sqrt(sum((x - mu) ** 2 for x in xs) / len(xs))


def pearson(a, b):
    """Correlation of two equal-length series; None when either is flat."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in pairs)
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-18 or syy <= 1e-18:
        return None
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def ann_sharpe(rets, bpy):
    """Annualized Sharpe of a per-bar net-return series. `bpy` = bars per year."""
    rets = [r for r in rets if r is not None]
    if len(rets) < 2:
        return 0.0
    sd = stdev(rets)
    if sd <= 1e-12:
        return 0.0
    return mean(rets) / sd * math.sqrt(bpy)


def logistic(x, center=0.0, scale=1.0):
    try:
        return 1.0 / (1.0 + math.exp(-(x - center) / scale))
    except OverflowError:
        return 0.0 if x < center else 1.0


# -------------------------------------------------------------- cache plumbing

def slice_cache(cache, a, b):
    """A view of the indicator cache over bars [a, b).

    Slicing (rather than recomputing indicators per window) is deliberate: the
    values were computed with their full leading history, so a window inherits a
    warm indicator instead of re-warming inside itself. No look-ahead — every
    indicator at bar i only ever used bars <= i.
    """
    return {k: v[a:b] for k, v in cache.items()}


def position_series(cache, fn, warmup=60, allow_short=True):
    """Desired position (+1/0/-1) at each bar. Strategies are stateless, so the
    signal at bar i IS the position held into bar i+1."""
    n = len(cache["close"])
    pos = [0] * n
    for i in range(min(warmup, n), n):
        t = fn(cache, i)
        if not allow_short and t < 0:
            t = 0
        pos[i] = t
    return pos


def bar_returns(pos, closes, cost):
    """Per-bar NET return series aligned to bars.

    Bar i earns the move from close[i-1] to close[i] on the position held over
    it (set at bar i-1), minus trading cost for any position change at bar i.
    """
    n = len(closes)
    out = [0.0] * n
    for i in range(1, n):
        prev = closes[i - 1]
        if prev:
            out[i] = pos[i - 1] * (closes[i] / prev - 1.0)
        turn = abs(pos[i] - pos[i - 1])
        if turn:
            out[i] -= cost * turn
    return out


def _connors_at(close, sma_trend, sma5, rsi2, entry):
    """Connors state machine at one (entry level, trend filter) pair."""
    pos, out = 0, []
    for i in range(len(close)):
        if None in (sma_trend[i], sma5[i], rsi2[i]):
            out.append(0)
            continue
        if pos == 0:
            if close[i] > sma_trend[i] and rsi2[i] < entry:
                pos = 1
        elif close[i] > sma5[i] or close[i] < sma_trend[i]:
            pos = 0
        out.append(pos)
    return out


# ------------------------------------------------------------------- the maps

class Analysis:
    """Runs every map over one symbol/interval sample.

    `windows` slices the sample into equal sequential blocks; `oos_frac` holds
    out the most recent fraction as an out-of-sample check (the only part of
    this that is not curve-fittable by construction).
    """

    def __init__(self, symbol, interval, candles, registry=None, *, windows=12,
                 fee_bps=10.0, slippage_bps=2.0, allow_short=True, warmup=60,
                 oos_frac=0.3, leverage=1.0, cache=None, extras=None,
                 include_benchmark=True):
        self.symbol = symbol
        self.interval = interval
        self.candles = candles
        self.registry = dict(registry or stratlib.REGISTRY)
        self.windows = max(3, int(windows))
        self.fee_bps = fee_bps
        self.slippage_bps = slippage_bps
        self.cost = (fee_bps + slippage_bps) / 1e4
        self.allow_short = allow_short
        self.warmup = warmup
        self.oos_frac = oos_frac
        self.leverage = leverage
        # a caller can supply its own indicator cache (the equity library builds a
        # different one) — otherwise the crypto precompute is used
        self.cache = stratlib.precompute(candles) if cache is None else cache
        self.extras = extras
        self.closes = self.cache["close"]
        self.n = len(candles)
        self.bpy = empirical_bars_per_year(candles, interval)
        self.bar_secs = median_bar_secs(candles)
        # Buy-and-hold is not a strategy, it is the bar every strategy has to clear.
        # Without it on the same axes, a long-biased rule in a rising market reads
        # as edge when it is just beta with extra steps.
        self.benchmark = "buy_hold" if include_benchmark else None
        if include_benchmark and "buy_hold" not in self.registry:
            self.registry["buy_hold"] = lambda cache, i: 1
        # per-strategy position + net return series (used by every per-bar map)
        self.pos = {}
        self.rets = {}
        for name, fn in self.registry.items():
            p = position_series(self.cache, fn, warmup=warmup,
                                allow_short=allow_short)
            self.pos[name] = p
            self.rets[name] = bar_returns(p, self.closes, self.cost)

    # -- helpers ----------------------------------------------------------
    def _run(self, a, b, fn):
        """Backtest one strategy over bars [a, b) and return annualized stats."""
        sub = slice_cache(self.cache, a, b)
        res = bt.run_signal(sub, fn, leverage=self.leverage, fee_bps=self.fee_bps,
                            slippage_bps=self.slippage_bps, warmup=1,
                            allow_short=self.allow_short)
        s = res.stats()
        eq = res.equity_curve
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
        s["ann_sharpe"] = round(ann_sharpe(rets, self.bpy), 2)
        s["bars"] = b - a
        return s

    def _bucket_stats(self, name, idx):
        """Aggregate a strategy's net returns over an arbitrary set of bars."""
        rs = [self.rets[name][i] for i in idx]
        if not rs:
            return {"bars": 0, "mean_bps": 0.0, "total_pct": 0.0, "sharpe": 0.0}
        total = 1.0
        for r in rs:
            total *= (1 + r)
        return {
            "bars": len(rs),
            "mean_bps": round(mean(rs) * 1e4, 3),
            "total_pct": round((total - 1) * 100, 2),
            "sharpe": round(ann_sharpe(rs, self.bpy), 2),
        }

    # -- 1. edge over time -------------------------------------------------
    def window_matrix(self):
        """strategy x time-slice. The decay map: an edge that only exists in
        column 1 is a memory, not a strategy."""
        a0 = self.warmup
        span = self.n - a0
        if span < self.windows * 20:
            self.windows = max(3, span // 20)
        step = span // self.windows
        bounds = [(a0 + k * step, a0 + (k + 1) * step if k < self.windows - 1 else self.n)
                  for k in range(self.windows)]
        cols = []
        for a, b in bounds:
            t0 = self.candles[a]["time"] / 1000.0
            t1 = self.candles[b - 1]["time"] / 1000.0
            cols.append({"start": a, "end": b, "t0": t0, "t1": t1})
        rows = []
        for name, fn in self.registry.items():
            cells = [self._run(a, b, fn) for a, b in bounds]
            rows.append({"strategy": name, "cells": cells})
        return {"cols": cols, "rows": rows, "metric": "total_return_pct"}

    # -- 2. where the edge lives ------------------------------------------
    def regime_matrix(self):
        """strategy x market regime (trend strength x volatility).

        This is the map that tells a mean-reversion strategy from a trend one:
        mean reversion should earn in LOW-ADX chop and bleed in strong trends;
        breakout does the opposite. If a strategy earns everywhere equally, be
        suspicious — that is usually drift, not edge.
        """
        adx = self.cache.get("adx")
        sma200 = self.cache.get("sma200")
        if adx is None and sma200 is None:
            return {"cols": [], "rows": []}
        atr = self.cache.get("atr") or indicators.atr(self.candles, 14)
        volp = [(atr[i] / self.closes[i] * 100) if (atr[i] and self.closes[i]) else None
                for i in range(self.n)]
        valid = sorted(v for v in volp[self.warmup:] if v is not None)
        if len(valid) < 10:
            return {"cols": [], "rows": []}
        lo_q, hi_q = valid[len(valid) // 3], valid[2 * len(valid) // 3]

        if adx is not None:
            trend_names = ("chop", "trend", "strong")

            def trend_of(i):
                a = adx[i]
                if a is None:
                    return None
                return "chop" if a < 20 else ("trend" if a < 30 else "strong")
        else:
            # equity regime: the 200-day line, split by whether it is itself rising
            trend_names = ("below-200", "above-200 flat", "above-200 rising")

            def trend_of(i):
                s200 = sma200[i]
                if s200 is None:
                    return None
                if self.closes[i] < s200:
                    return "below-200"
                prev = sma200[i - 21] if i >= 21 else None
                if prev is None:
                    return None
                return "above-200 rising" if s200 > prev else "above-200 flat"

        def vol_of(i):
            v = volp[i]
            if v is None:
                return None
            return "lo-vol" if v <= lo_q else ("mid-vol" if v <= hi_q else "hi-vol")

        combos = [(t, v) for t in trend_names
                  for v in ("lo-vol", "mid-vol", "hi-vol")]
        buckets = {c: [] for c in combos}
        for i in range(self.warmup, self.n):
            t, v = trend_of(i), vol_of(i)
            if t and v:
                buckets[(t, v)].append(i)
        cols = [{"label": f"{t}/{v}", "trend": t, "vol": v, "bars": len(buckets[(t, v)])}
                for t, v in combos]
        rows = []
        for name in self.registry:
            cells = [self._bucket_stats(name, buckets[(t, v)]) for t, v in combos]
            rows.append({"strategy": name, "cells": cells})
        return {"cols": cols, "rows": rows, "metric": "mean_bps",
                "vol_quantiles": [round(lo_q, 3), round(hi_q, 3)]}

    # -- 3. session effects -------------------------------------------------
    def hour_matrix(self):
        """strategy x hour-of-day (UTC). Only meaningful on intraday bars."""
        if self.bar_secs > 3600:          # daily bars have one hour, not twenty-four
            return {"cols": [], "rows": []}
        buckets = {h: [] for h in range(24)}
        for i in range(self.warmup, self.n):
            h = int((self.candles[i]["time"] / 1000) // 3600 % 24)
            buckets[h].append(i)
        cols = [{"label": f"{h:02d}", "bars": len(buckets[h])} for h in range(24)]
        rows = [{"strategy": name,
                 "cells": [self._bucket_stats(name, buckets[h]) for h in range(24)]}
                for name in self.registry]
        return {"cols": cols, "rows": rows, "metric": "mean_bps"}

    # -- 4. edge or cost illusion? -----------------------------------------
    def cost_matrix(self, levels=(0.0, 2.0, 5.0, 10.0, 15.0, 20.0, 30.0)):
        """strategy x round-trip cost (bps per side).

        The most honest column in the report. A strategy whose return collapses
        between 0 and 10 bps never had an edge — it had a fee subsidy that does
        not exist. High-frequency strategies die first, which is the point.
        """
        cols = [{"label": f"{c:g} bps", "bps": c} for c in levels]
        rows = []
        for name, fn in self.registry.items():
            cells = []
            for c in levels:
                res = bt.run_signal(self.cache, fn, leverage=self.leverage,
                                    fee_bps=c, slippage_bps=0.0,
                                    warmup=self.warmup, allow_short=self.allow_short)
                s = res.stats()
                eq = res.equity_curve
                rr = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
                s["ann_sharpe"] = round(ann_sharpe(rr, self.bpy), 2)
                cells.append(s)
            rows.append({"strategy": name, "cells": cells})
        return {"cols": cols, "rows": rows, "metric": "total_return_pct"}

    # -- 5. are these actually different trades? ---------------------------
    def corr_matrix(self):
        """Correlation of per-bar NET returns. Two strategies at r > 0.8 are one
        position with two names — stacking them doubles risk, not edge."""
        names = list(self.registry)
        sl = slice(self.warmup, self.n)
        series = {n: self.rets[n][sl] for n in names}
        m = [[(1.0 if a == b else pearson(series[a], series[b])) for b in names]
             for a in names]
        return {"names": names, "matrix": m}

    def overlap_matrix(self):
        """Cosine similarity of the position vectors: +1 = same trade, -1 =
        opposite side, 0 = independent exposure."""
        names = list(self.registry)
        sl = slice(self.warmup, self.n)
        vecs = {n: self.pos[n][sl] for n in names}
        norms = {n: math.sqrt(sum(v * v for v in vecs[n])) for n in names}
        m = []
        for a in names:
            row = []
            for b in names:
                if norms[a] <= 0 or norms[b] <= 0:
                    row.append(None)
                else:
                    dot = sum(x * y for x, y in zip(vecs[a], vecs[b]))
                    row.append(max(-1.0, min(1.0, dot / (norms[a] * norms[b]))))
            m.append(row)
        return {"names": names, "matrix": m}

    # -- 6. robustness (parameter plateaus) --------------------------------
    def sweep_rsi(self, os_levels=(15, 20, 25, 30, 35, 40),
                  ob_levels=(60, 65, 70, 75, 80, 85)):
        """RSI mean reversion over its threshold grid.

        Read it as topography: a broad warm plateau means the edge survives the
        parameter you picked being slightly wrong. A single hot cell surrounded
        by cold ones is overfitting with a nice color.
        """
        cols = [{"label": str(ob), "ob": ob} for ob in ob_levels]
        rows = []
        for os_ in os_levels:
            cells = []
            for ob in ob_levels:
                def fn(cache, i, _os=os_, _ob=ob):
                    r = cache["rsi"][i]
                    if r is None:
                        return 0
                    if r <= _os:
                        return 1
                    if r >= _ob:
                        return -1
                    return 0
                res = bt.run_signal(self.cache, fn, leverage=self.leverage,
                                    fee_bps=self.fee_bps, slippage_bps=self.slippage_bps,
                                    warmup=self.warmup, allow_short=self.allow_short)
                s = res.stats()
                eq = res.equity_curve
                rr = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
                s["ann_sharpe"] = round(ann_sharpe(rr, self.bpy), 2)
                cells.append(s)
            rows.append({"strategy": f"oversold {os_}", "cells": cells})
        return {"cols": cols, "rows": rows, "metric": "total_return_pct",
                "x_title": "overbought threshold", "y_title": "oversold threshold"}

    def sweep_vwap(self, ks=(0.001, 0.002, 0.003, 0.004, 0.006, 0.008, 0.010, 0.015)):
        """VWAP mean reversion across its band width (fraction from VWAP)."""
        cols = [{"label": f"{k*100:g}%", "k": k} for k in ks]
        cells = []
        for k in ks:
            def fn(cache, i, _k=k):
                return stratlib.vwap_reversion(cache, i, k=_k)
            res = bt.run_signal(self.cache, fn, leverage=self.leverage,
                                fee_bps=self.fee_bps, slippage_bps=self.slippage_bps,
                                warmup=self.warmup, allow_short=self.allow_short)
            s = res.stats()
            eq = res.equity_curve
            rr = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
            s["ann_sharpe"] = round(ann_sharpe(rr, self.bpy), 2)
            cells.append(s)
        return {"cols": cols, "rows": [{"strategy": "vwap_reversion", "cells": cells}],
                "metric": "total_return_pct", "x_title": "band width from VWAP"}

    def _stats_of(self, signal_fn):
        """One full-sample backtest, annualized."""
        res = bt.run_signal(self.cache, signal_fn, leverage=self.leverage,
                            fee_bps=self.fee_bps, slippage_bps=self.slippage_bps,
                            warmup=self.warmup, allow_short=self.allow_short)
        st = res.stats()
        eq = res.equity_curve
        rr = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
        st["ann_sharpe"] = round(ann_sharpe(rr, self.bpy), 2)
        return st

    def sweep_connors(self, thresholds=(5, 10, 15, 20, 25, 30),
                      trend_lens=(50, 100, 150, 200, 250, 300)):
        """Connors RSI(2) across entry level x trend-filter length.

        The flagship equity mean-reversion rule, re-run over its own grid. Both
        axes are things a person picks arbitrarily; if the result only works at
        one pair, the person picked the answer, not the rule.
        """
        close, rsi2 = self.cache["close"], self.cache["rsi2"]
        smas = {L: indicators.sma(close, L) for L in trend_lens}
        sma5 = self.cache["sma5"]
        cols = [{"label": str(L), "trend_len": L} for L in trend_lens]
        rows = []
        for thr in thresholds:
            cells = []
            for L in trend_lens:
                state = _connors_at(close, smas[L], sma5, rsi2, thr)
                cells.append(self._stats_of(lambda c, i, _s=state: _s[i]))
            rows.append({"strategy": f"RSI2 < {thr}", "cells": cells})
        return {"cols": cols, "rows": rows, "metric": "total_return_pct",
                "title": "Connors RSI(2) robustness - entry level x trend filter",
                "unit": "%", "fmt": 1, "col_title": "entry \\ trend SMA"}

    def sweep_ibs(self, levels=(0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40)):
        """Internal Bar Strength threshold — the one-day reversion knob."""
        sma200, ibs, close = self.cache["sma200"], self.cache["ibs"], self.cache["close"]
        cols = [{"label": f"{L:g}", "level": L} for L in levels]
        cells = []
        for L in levels:
            def fn(c, i, _L=L):
                if ibs[i] is None or sma200[i] is None:
                    return 0
                return 1 if (ibs[i] < _L and close[i] > sma200[i]) else 0
            cells.append(self._stats_of(fn))
        return {"cols": cols, "rows": [{"strategy": "ibs_reversion", "cells": cells}],
                "metric": "total_return_pct", "unit": "%", "fmt": 1,
                "title": "IBS reversion robustness - close-in-range threshold",
                "col_title": "IBS threshold"}

    def sweep_grid(self, name, spec, in_sample_only=False):
        """One rule's whole parameter grid, backtested cell by cell."""
        xlab, xs = spec["x"]
        ylab, ys = spec["y"] if spec["y"] else (None, [None])
        split = self._split()
        rows = []
        for y in ys:
            cells = []
            for x in xs:
                pos = spec["make"](self.cache, x, y)
                fn = lambda c, i, _p=pos: _p[i]
                if in_sample_only:
                    cells.append({**self._run(self.warmup, split, fn),
                                  "params": {"x": x, "y": y}})
                else:
                    cells.append({**self._stats_of(fn), "params": {"x": x, "y": y}})
            rows.append({"strategy": (f"{ylab} {y}" if ylab else name),
                         "cells": cells})
        return {"cols": [{"label": str(x), "value": x} for x in xs],
                "rows": rows, "metric": "total_return_pct", "unit": "%", "fmt": 1,
                "title": f"{name} — {xlab}" + (f" x {ylab}" if ylab else ""),
                "col_title": (f"{ylab} \\ {xlab}" if ylab else xlab),
                "strategy": name}

    def _split(self):
        return int(self.warmup + (self.n - self.warmup) * (1 - self.oos_frac))

    def sweep_all(self, specs, in_sample_only=True):
        """Every rule's grid. Selection uses IN-SAMPLE cells only — choosing a
        parameter by its out-of-sample result and then reporting that result as
        out-of-sample is the oldest way to fool yourself in this business."""
        return [self.sweep_grid(n, sp, in_sample_only=in_sample_only)
                for n, sp in specs.items()]

    def sweeps(self):
        """Whichever parameter grids the loaded strategy family actually has."""
        out = []
        if "rsi2" in self.cache:
            from . import strats_equity as eqlib
            return self.sweep_all(eqlib.sweep_specs(self.cache), in_sample_only=False)
        if "rsi" in self.cache:
            out.append({**self.sweep_rsi(),
                        "title": "RSI reversion robustness - oversold x overbought",
                        "unit": "%", "fmt": 1, "col_title": "oversold \\ overbought"})
            out.append({**self.sweep_vwap(),
                        "title": "VWAP reversion robustness - band width",
                        "unit": "%", "fmt": 1, "col_title": "band"})
        return out

    def decomposition(self, wm):
        """Where the market's own return came from: overnight vs the day session.

        Equity-only and cost-free by construction — this is a decomposition of the
        instrument, not a tradeable strategy (capturing the overnight leg means
        paying the spread twice a day, which the cost map prices separately).
        US equities have historically put most of their return in the overnight
        gap; if that holds here, a close-to-close strategy is fighting the part of
        the day that does not pay.
        """
        if "open" not in self.cache or not wm.get("cols"):
            return {"cols": [], "rows": []}
        op, cl = self.cache["open"], self.closes
        legs = {
            "overnight (close->open)": [0.0] + [(op[i] / cl[i - 1] - 1) if cl[i - 1] else 0.0
                                                for i in range(1, self.n)],
            "intraday (open->close)": [(cl[i] / op[i] - 1) if op[i] else 0.0
                                       for i in range(self.n)],
            "buy & hold (close->close)": [0.0] + [(cl[i] / cl[i - 1] - 1) if cl[i - 1] else 0.0
                                                  for i in range(1, self.n)],
        }
        cols = [{"label": time.strftime("%Y-%m", time.gmtime(c["t0"])), **c}
                for c in wm["cols"]]
        rows = []
        for name, ser in legs.items():
            cells = []
            for c in wm["cols"]:
                seg = ser[c["start"]:c["end"]]
                tot = 1.0
                for r in seg:
                    tot *= (1 + r)
                cells.append({"total_return_pct": round((tot - 1) * 100, 2),
                              "ann_sharpe": round(ann_sharpe(seg, self.bpy), 2),
                              "bars": len(seg)})
            rows.append({"strategy": name, "cells": cells})
        return {"cols": cols, "rows": rows, "metric": "total_return_pct"}

    # -- 7. the answer: which edge is still ACTIVE --------------------------
    def active_edge(self, wm=None):
        """Fold the maps into one ranking: recency-weighted, consistency-checked,
        out-of-sample-verified.

        Score (0-100) = 100 * reliability * weighted blend of
            0.30  p(recency-weighted window Sharpe)   is it working LATELY
            0.25  p(out-of-sample Sharpe)             does it hold on held-out bars
            0.20  hit rate across windows             is it consistent
            0.15  improving-vs-decaying term          which way is it heading
            0.10  p(full-sample Sharpe)               did it ever work at all
        where p() is a logistic centered on a Sharpe hurdle of 1.0, then scaled by
        a reliability factor (too few trades to trust) and a consistency
        multiplier (0.6 + 0.4 x hit rate), so a strategy carried by two lucky
        windows cannot outrank one that keeps working.
        A strategy that loses out-of-sample is capped at 45 no matter what the
        in-sample numbers say.

        This is a RANKING HEURISTIC, not proof of edge. It cannot see regime
        change, funding, latency, or the fact that you are not the only one
        who ran this test.
        """
        wm = wm or self.window_matrix()
        k = len(wm["cols"])
        half_life = max(1.0, k / 4.0)
        wts = [0.5 ** ((k - 1 - j) / half_life) for j in range(k)]
        wsum = sum(wts) or 1.0

        split = int(self.warmup + (self.n - self.warmup) * (1 - self.oos_frac))

        # the benchmark's own numbers, on identical bars and identical costs
        bench = None
        if self.benchmark and self.benchmark in self.registry:
            bfull = self._run(self.warmup, self.n, self.registry[self.benchmark])
            boos = self._run(split, self.n, self.registry[self.benchmark])
            bench = {"full_sharpe": bfull["ann_sharpe"],
                     "full_return_pct": bfull["total_return_pct"],
                     "oos_sharpe": boos["ann_sharpe"],
                     "oos_return_pct": boos["total_return_pct"]}

        out = []
        for row in wm["rows"]:
            name = row["strategy"]
            cells = row["cells"]
            sh = [c["ann_sharpe"] for c in cells]
            rets = [c["total_return_pct"] for c in cells]
            trades = sum(c["trades"] for c in cells)
            recent = sum(s * w for s, w in zip(sh, wts)) / wsum
            hit = sum(1 for r in rets if r > 0) / max(1, len(rets))
            third = max(1, k // 3)
            decay = mean(sh[-third:]) - mean(sh[:third])

            full = self._run(self.warmup, self.n, self.registry[name])
            is_ = self._run(self.warmup, split, self.registry[name])
            oos = self._run(split, self.n, self.registry[name])

            exposure = (sum(1 for p in self.pos[name][self.warmup:] if p != 0)
                        / max(1, self.n - self.warmup))
            # "Too few bets to trust" has to scale with the bar size. 30 trades is
            # a fair ask of a crypto minute strategy and an absurd one of a daily
            # rule that holds for months — 15 years of dailies is ~50 trades for a
            # 200-day trend rule, and that is the rule working as designed. The
            # benchmark is exempt: buy-and-hold makes one bet by construction, and
            # its Sharpe is estimated from every bar, not from its trade count.
            min_trades = 30.0 if self.bar_secs <= 3600 else 12.0
            thin_trades = 20 if self.bar_secs <= 3600 else 8
            rel = 1.0 if name == self.benchmark else (
                min(1.0, trades / min_trades) if trades else 0.0)
            blend = (0.30 * logistic(recent, 1.0, 0.7)
                     + 0.25 * logistic(oos["ann_sharpe"], 1.0, 0.7)
                     + 0.20 * hit
                     + 0.15 * logistic(decay, 0.0, 1.0)
                     + 0.10 * logistic(full["ann_sharpe"], 1.0, 0.7))
            # consistency multiplier: a strategy that only worked in a couple of
            # windows cannot outrank one that works in most of them, however good
            # those couple of windows were.
            score = 100.0 * rel * blend * (0.6 + 0.4 * hit)
            if oos["total_return_pct"] <= 0:
                score = min(score, 45.0)

            beats_bench = None
            if bench and name != self.benchmark:
                # risk-adjusted, on the same bars: matching buy-and-hold with a
                # rule is not edge, it is beta you paid commissions for
                beats_bench = (full["ann_sharpe"] > bench["full_sharpe"]
                               and oos["ann_sharpe"] > bench["oos_sharpe"])

            if name == self.benchmark:
                verdict = "BENCHMARK"
            elif trades < thin_trades:
                verdict = "THIN SAMPLE"
            elif beats_bench is False and full["total_return_pct"] > 0:
                verdict = "BETA ONLY"
            elif score >= 65 and oos["ann_sharpe"] > 0 and hit >= 0.5:
                verdict = "ACTIVE EDGE"
            elif score >= 50:
                verdict = "WEAK / WATCH"
            elif full["total_return_pct"] > 0 and decay < -0.5:
                verdict = "FADING"
            else:
                verdict = "NO EDGE"

            out.append({
                "strategy": name,
                "score": round(score, 1),
                "verdict": verdict,
                "recent_sharpe": round(recent, 2),
                "full_sharpe": full["ann_sharpe"],
                "is_sharpe": is_["ann_sharpe"],
                "oos_sharpe": oos["ann_sharpe"],
                "oos_return_pct": oos["total_return_pct"],
                "full_return_pct": full["total_return_pct"],
                "hit_rate": round(hit, 2),
                "decay": round(decay, 2),
                "trades": trades,
                "max_dd_pct": full["max_drawdown_pct"],
                "profit_factor": full["profit_factor"],
                "exposure": round(exposure, 3),
                "beats_benchmark": beats_bench,
                "is_benchmark": name == self.benchmark,
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return {"rows": out, "oos_split_bar": split, "benchmark": bench,
                "benchmark_name": self.benchmark,
                "oos_bars": self.n - split, "windows": k}

    # -- everything, once ---------------------------------------------------
    def run_all(self):
        wm = self.window_matrix()
        return {
            "meta": {
                "symbol": self.symbol, "interval": self.interval,
                "candles": self.n,
                "t0": self.candles[0]["time"] / 1000.0,
                "t1": self.candles[-1]["time"] / 1000.0,
                "fee_bps": self.fee_bps, "slippage_bps": self.slippage_bps,
                "allow_short": self.allow_short, "leverage": self.leverage,
                "warmup": self.warmup, "oos_frac": self.oos_frac,
                "bars_per_year": round(self.bpy, 1),
                "bar_secs": self.bar_secs,
                "strategies": [n for n in self.registry if n != self.benchmark],
                "benchmark": self.benchmark,
            },
            "windows": wm,
            "regimes": self.regime_matrix(),
            "hours": self.hour_matrix(),
            "costs": self.cost_matrix(),
            "correlation": self.corr_matrix(),
            "overlap": self.overlap_matrix(),
            "sweeps": self.sweeps(),
            "decomposition": self.decomposition(wm),
            "edge": self.active_edge(wm),
        }


def print_edge_table(report):
    """Console version of the headline ranking."""
    meta, edge = report["meta"], report["edge"]
    print(f"\n=== ACTIVE EDGE {meta['symbol']} {meta['interval']} "
          f"({meta['candles']} candles, {edge['windows']} windows, "
          f"{meta['fee_bps']}+{meta['slippage_bps']} bps costs) ===")
    print(f"  {'strategy':<20}{'score':>7}{'verdict':>14}{'fullSh':>8}"
          f"{'oosSh':>8}{'oosRet%':>9}{'hit':>6}{'expo':>6}{'maxDD%':>8}{'trades':>8}")
    for r in edge["rows"]:
        mark = "*" if r.get("is_benchmark") else " "
        print(f"  {mark}{r['strategy']:<19}{r['score']:>7}{r['verdict']:>14}"
              f"{r['full_sharpe']:>8}{r['oos_sharpe']:>8}"
              f"{r['oos_return_pct']:>9}{r['hit_rate']:>6}"
              f"{r.get('exposure', 0):>6}{r['max_dd_pct']:>8}{r['trades']:>8}")
    b = edge.get("benchmark")
    if b:
        print(f"  * = benchmark. Buy & hold on the same bars and costs: "
              f"Sharpe {b['full_sharpe']}, OOS Sharpe {b['oos_sharpe']}, "
              f"OOS return {b['oos_return_pct']}%.")
        print("  'BETA ONLY' = positive, but does not beat buy & hold on Sharpe "
              "in-sample AND out-of-sample.")
    print(f"  expo = fraction of bars holding a position; "
          f"annualization = {meta.get('bars_per_year', '?')} bars/year "
          "(measured from the timestamps, not assumed).")
    print(f"  Sharpe = annualized, net of costs. OOS = last "
          f"{int(meta['oos_frac']*100)}% of bars ({edge['oos_bars']} candles), "
          "held out of every other column.")
    print("  score is a RANKING HEURISTIC over a single sample — re-run it on a "
          "different\n  date range and a different symbol before trusting any row.")
