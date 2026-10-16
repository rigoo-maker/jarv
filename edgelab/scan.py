"""The scan: discover in-sample, validate out-of-sample, pay for the search."""

from __future__ import annotations

import csv
from collections import defaultdict

from .signals import SIGNALS, buy_and_hold
from .stats import perf, welch_t, bonferroni_t, deflated_sharpe

ZERO_TURNOVER = {"buy&hold", "trend200"}


def load_csv(path):
    """-> {symbol: [bar, ...]} sorted by date, gap-fill bars dropped."""
    out = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            if str(r.get("interpolated", "False")).lower() == "true":
                continue
            out[r["symbol"]].append({
                "date": r["date"], "open": float(r["open"]),
                "high": float(r["high"]), "low": float(r["low"]),
                "close": float(r["close"]), "volume": float(r["volume"]),
            })
    for s in out:
        out[s].sort(key=lambda b: b["date"])
    return dict(out)


def scan(data, split_date, symbols=None, signals=None, financing_rate=0.05):
    """Run every (signal, symbol) pair; report IS and OOS side by side."""
    symbols = symbols or sorted(data)
    signals = signals or SIGNALS
    rows = []
    for name, fn in signals.items():
        for sym in symbols:
            series = fn(data[sym], symbol=sym)
            is_ = [x for x in series if x[0] < split_date]
            oos = [x for x in series if x[0] >= split_date]
            if len(is_) < 30 or len(oos) < 30:
                continue
            a = perf([d for d, _ in is_], [r for _, r in is_])
            b = perf([d for d, _ in oos], [r for _, r in oos])
            if a and b:
                rows.append({"signal": name, "symbol": sym, "is": a, "oos": b})
    n = len(rows)
    thresh = bonferroni_t(n)
    for r in rows:
        r["is"]["deflated_sharpe"] = deflated_sharpe(
            r["is"]["sharpe"], n, r["is"]["n"])
        r["clears_bonferroni"] = abs(r["is"]["t_stat"]) >= thresh
    rows.sort(key=lambda r: -r["is"]["sharpe"])
    return {"rows": rows, "n_tests": n, "bonferroni_t": thresh,
            "survivors": [r for r in rows if r["clears_bonferroni"]]}


def increment_test(data, signal_fn, split_date, symbols=None, **kw):
    """Pool one signal across symbols and test its INCREMENT over buy&hold.

    Pooling is the honest way to gain statistical power: it is a single
    hypothesis evaluated on many observations, rather than many hypotheses of
    which you report the luckiest. If an effect is real it survives pooling;
    if it only appears on one symbol, it was selection.
    """
    symbols = symbols or sorted(data)
    sig, base = [], []
    for sym in symbols:
        for d, r in signal_fn(data[sym], symbol=sym, **kw):
            if r != 0.0:
                sig.append((d, r))
        for d, r in buy_and_hold(data[sym], symbol=sym):
            base.append((d, r))
    out = {}
    for label, keep in (("in_sample", lambda d: d < split_date),
                        ("out_of_sample", lambda d: d >= split_date),
                        ("full", lambda d: True)):
        s = [r for d, r in sig if keep(d)]
        b = [r for d, r in base if keep(d)]
        out[label] = welch_t(s, b) if len(s) > 2 and len(b) > 2 else None
    return out


def acid_test(data, candidates, benchmark, split_date, financing_rate=0.05):
    """Lever each candidate to the benchmark's volatility and charge financing.

    An unlevered Sharpe is not a return. A low-volatility, high-Sharpe
    strategy only beats the benchmark if you can lever it there, and the
    borrowing cost of doing so is usually what decides the question. Any
    comparison that omits it flatters low-vol strategies.
    """
    def oos(series):
        o = [x for x in series if x[0] >= split_date]
        return [d for d, _ in o], [r for _, r in o]

    bd, br = oos(benchmark["series"])
    bench = perf(bd, br)
    results = [{"name": benchmark["name"], "leverage": 1.0, "perf": bench,
                "vs_benchmark_pp": 0.0}]
    for c in candidates:
        d, r = oos(c["series"])
        base = perf(d, r)
        if not base or base["vol_pct"] <= 0:
            continue
        lev = bench["vol_pct"] / base["vol_pct"]
        levered = perf(d, r, leverage=lev, financing_rate=financing_rate)
        results.append({"name": c["name"], "leverage": lev, "perf": levered,
                        "unlevered": base,
                        "vs_benchmark_pp": levered["cagr_pct"] - bench["cagr_pct"]})
    return {"benchmark": bench, "results": results,
            "financing_rate": financing_rate}
