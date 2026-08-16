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
    return YEAR_SECS / INTERVAL_SECS.get(interval, 60)


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


def ann_sharpe(rets, interval):
    """Annualized Sharpe of a per-bar net-return series."""
    rets = [r for r in rets if r is not None]
    if len(rets) < 2:
        return 0.0
    sd = stdev(rets)
    if sd <= 1e-12:
        return 0.0
    return mean(rets) / sd * math.sqrt(bars_per_year(interval))


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


# ------------------------------------------------------------------- the maps

class Analysis:
    """Runs every map over one symbol/interval sample.

    `windows` slices the sample into equal sequential blocks; `oos_frac` holds
    out the most recent fraction as an out-of-sample check (the only part of
    this that is not curve-fittable by construction).
    """

    def __init__(self, symbol, interval, candles, registry=None, *, windows=12,
                 fee_bps=10.0, slippage_bps=2.0, allow_short=True, warmup=60,
                 oos_frac=0.3, leverage=1.0):
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
        self.cache = stratlib.precompute(candles)
        self.closes = self.cache["close"]
        self.n = len(candles)
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
        s["ann_sharpe"] = round(ann_sharpe(rets, self.interval), 2)
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
            "sharpe": round(ann_sharpe(rs, self.interval), 2),
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
        adx = self.cache["adx"]
        atr = indicators.atr(self.candles, 14)
        volp = [(atr[i] / self.closes[i] * 100) if (atr[i] and self.closes[i]) else None
                for i in range(self.n)]
        valid = sorted(v for v in volp[self.warmup:] if v is not None)
        if len(valid) < 10:
            return {"cols": [], "rows": []}
        lo_q, hi_q = valid[len(valid) // 3], valid[2 * len(valid) // 3]

        def trend_of(i):
            a = adx[i]
            if a is None:
                return None
            return "chop" if a < 20 else ("trend" if a < 30 else "strong")

        def vol_of(i):
            v = volp[i]
            if v is None:
                return None
            return "lo-vol" if v <= lo_q else ("mid-vol" if v <= hi_q else "hi-vol")

        combos = [(t, v) for t in ("chop", "trend", "strong")
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
        if INTERVAL_SECS.get(self.interval, 60) > 3600:
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
                s["ann_sharpe"] = round(ann_sharpe(rr, self.interval), 2)
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
                s["ann_sharpe"] = round(ann_sharpe(rr, self.interval), 2)
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
            s["ann_sharpe"] = round(ann_sharpe(rr, self.interval), 2)
            cells.append(s)
        return {"cols": cols, "rows": [{"strategy": "vwap_reversion", "cells": cells}],
                "metric": "total_return_pct", "x_title": "band width from VWAP"}

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

            rel = min(1.0, trades / 30.0) if trades else 0.0
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

            if trades < 20:
                verdict = "THIN SAMPLE"
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
            })
        out.sort(key=lambda r: r["score"], reverse=True)
        return {"rows": out, "oos_split_bar": split,
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
            },
            "windows": wm,
            "regimes": self.regime_matrix(),
            "hours": self.hour_matrix(),
            "costs": self.cost_matrix(),
            "correlation": self.corr_matrix(),
            "overlap": self.overlap_matrix(),
            "sweep_rsi": self.sweep_rsi(),
            "sweep_vwap": self.sweep_vwap(),
            "edge": self.active_edge(wm),
        }


def print_edge_table(report):
    """Console version of the headline ranking."""
    meta, edge = report["meta"], report["edge"]
    print(f"\n=== ACTIVE EDGE {meta['symbol']} {meta['interval']} "
          f"({meta['candles']} candles, {edge['windows']} windows, "
          f"{meta['fee_bps']}+{meta['slippage_bps']} bps costs) ===")
    print(f"  {'strategy':<18}{'score':>7}{'verdict':>14}{'recentSh':>10}"
          f"{'oosSh':>8}{'oosRet%':>9}{'hit':>6}{'decay':>7}{'trades':>8}")
    for r in edge["rows"]:
        print(f"  {r['strategy']:<18}{r['score']:>7}{r['verdict']:>14}"
              f"{r['recent_sharpe']:>10}{r['oos_sharpe']:>8}"
              f"{r['oos_return_pct']:>9}{r['hit_rate']:>6}{r['decay']:>7}"
              f"{r['trades']:>8}")
    print(f"  Sharpe = annualized, net of costs. OOS = last "
          f"{int(meta['oos_frac']*100)}% of bars ({edge['oos_bars']} candles), "
          "held out of every other column.")
    print("  score is a RANKING HEURISTIC over a single sample — re-run it on a "
          "different\n  date range and a different symbol before trusting any row.")
