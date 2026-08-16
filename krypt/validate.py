"""Does the edge survive being tested properly?

Ranking strategies produces a winner by construction. These three tests are the
cheapest ways to find out whether the winner is real, and they kill most of them:

1. **Execution delay.** The backtest fills at the close of the bar that produced
   the signal — you knew the close and traded at it. Here the fill moves to the
   NEXT bar's open. Rules whose edge lives inside that one bar (most fast
   reversion) collapse. If a strategy needs the close it just saw, it is not
   tradeable.

2. **Timing permutation.** Take the exact position series — same number of
   trades, same holding periods, same exposure — and slide it to a random point
   in the price history. Repeat a few hundred times and you get the distribution
   of results from a strategy with identical *shape* and no *timing*. If the real
   Sharpe sits inside that distribution, the timing carried no information: the
   result came from being long an instrument that went up.

3. **Multiple testing.** Ten rules x hundreds of parameter cells x hundreds of
   hybrids is not one test, it is hundreds, and the best of hundreds looks
   excellent under the null. Benjamini-Hochberg controls the false-discovery rate
   across everything tested in the run, and the Bonferroni threshold is reported
   as the stricter alternative.

What this still cannot do: prove an edge. It can only fail to kill one. The
things it does not see are regime change, capacity, borrow, and the fact that
you are not the only person who has run this test.
"""

from __future__ import annotations

import math
import random

from . import analytics as ana


def delayed_returns(candles, pos, cost):
    """Per-bar net returns when the fill happens at the NEXT bar's open.

    Signal at bar i is filled at the open of bar i+1 and held to the open of
    i+2, which is what a bar-close strategy can actually achieve.
    """
    n = len(candles)
    out = [0.0] * n
    for i in range(2, n):
        held = pos[i - 2]
        o_prev, o_now = candles[i - 1].get("open"), candles[i].get("open")
        if o_prev and o_now and held:
            out[i] = held * (o_now / o_prev - 1)
        turn = abs(pos[i - 1] - pos[i - 2])
        if turn:
            out[i] -= cost * turn
    return out


def close_returns(candles, pos, cost):
    closes = [c["close"] for c in candles]
    return ana.bar_returns(pos, closes, cost)


def rotation_pvalue(pos, mkt_rets, bpy, *, warmup=0, samples=200, seed=7):
    """p-value from sliding the position pattern to random points in history.

    The null keeps everything about the strategy except when it happened: same
    trade count, same holding periods, same exposure, wrong dates. A p of 0.30
    means three runs in ten of a strategy with this shape and no timing skill
    beat it.
    """
    # The position set at bar i earns bar i+1's move — the same next-bar
    # convention as the backtester. Pairing pos[i] with mkt[i] would credit a
    # position with the move that CREATED its signal, which is look-ahead
    # pointing backwards and makes every mean-reversion rule look terrible.
    live = pos[warmup:-1]
    mkt = mkt_rets[warmup + 1:]
    m = min(len(live), len(mkt))
    live, mkt = live[:m], mkt[:m]
    if m < 30 or not any(live):
        return {"p_value": 1.0, "samples": 0, "observed": 0.0, "null_median": 0.0,
                "note": "never in the market"}
    if len(set(live)) == 1:
        # A constant position has no timing to test — rotating it returns the
        # same series, and the only differences are floating-point noise, which
        # would otherwise be reported as a p-value around 0.5.
        return {"p_value": 1.0, "samples": 0,
                "observed": round(ana.ann_sharpe([live[i] * mkt[i] for i in range(m)],
                                                 bpy), 3),
                "null_median": 0.0, "note": "constant exposure - no timing to test"}

    # Fast path for long/flat rules: a rotation only moves WHICH bars are
    # selected, so summing over the selected indices is O(bars in market) instead
    # of O(all bars). Low-exposure strategies — the interesting ones — get
    # thousands of permutations for the price of a few hundred, which matters
    # because the smallest p a permutation test can report is 1/(samples+1), and
    # a multiple-testing correction over hundreds of hypotheses needs far below
    # that to resolve anything at all.
    binary = set(live) <= {0, 1}
    sq = [x * x for x in mkt] if binary else None
    root = math.sqrt(bpy)

    def sharpe_from(sum1, sum2):
        mean = sum1 / m
        var = sum2 / m - mean * mean
        if var <= 1e-24:
            return 0.0
        return mean / math.sqrt(var) * root

    def sharpe_of(p):
        rets = [p[i] * mkt[i] for i in range(m)]
        return ana.ann_sharpe(rets, bpy)

    rng = random.Random(seed)
    if binary:
        idx = [i for i, v in enumerate(live) if v]
        observed = sharpe_from(sum(mkt[i] for i in idx), sum(sq[i] for i in idx))
        null = []
        for _ in range(samples):
            off = rng.randrange(m)
            s1 = s2 = 0.0
            for i in idx:
                j = i + off
                if j >= m:
                    j -= m
                s1 += mkt[j]
                s2 += sq[j]
            null.append(sharpe_from(s1, s2))
    else:
        observed = sharpe_of(live)
        null = []
        for _ in range(samples):
            off = rng.randrange(m)
            rotated = live[off:] + live[:off]
            null.append(sharpe_of(rotated))
    beat = sum(1 for x in null if x >= observed - 1e-12)
    null_sorted = sorted(null)
    return {
        "p_value": round((1 + beat) / (samples + 1), 6),
        "p_floor": round(1.0 / (samples + 1), 6),
        "samples": samples,
        "observed": round(observed, 3),
        "null_median": round(null_sorted[len(null_sorted) // 2], 3),
        "null_p95": round(null_sorted[int(len(null_sorted) * 0.95)], 3),
    }


def benjamini_hochberg(pvals, alpha=0.05, m_total=None):
    """Return (rejected flags, adjusted p-values) controlling the false-discovery
    rate — the right correction when you are screening many candidates and want
    the survivors, not a single yes/no."""
    m = m_total or len(pvals)
    if not pvals:
        return [], []
    k = len(pvals)
    order = sorted(range(k), key=lambda i: pvals[i])
    adj = [0.0] * k
    prev = 1.0
    for rank, idx in enumerate(reversed(order), start=1):
        i = k - rank + 1        # this candidate's rank among those tested
        val = min(prev, pvals[idx] * m / i)
        adj[idx] = round(min(1.0, val), 4)
        prev = val
    rejected = [adj[i] <= alpha for i in range(k)]
    return rejected, adj


def gauntlet(candles, positions_by_name, *, bpy, warmup=300, cost=0.0003,
             samples=200, alpha=0.05, benchmark=None, seed=7, m_total=None,
             oos_start=None):
    """Run all three tests over every candidate and rank the survivors.

    `m_total` is the number of hypotheses the RUN actually explored, which is
    usually far larger than the shortlist handed to this function: screening 300
    combinations and then correcting as if 20 were tested is the multiple-testing
    mistake with an extra step.
    """
    # A correction over m hypotheses cannot resolve anything unless the test can
    # report p below alpha/m. Scale the permutation count to the family size
    # instead of silently returning "nothing is significant".
    needed = int((m_total or len(positions_by_name)) / max(alpha, 1e-9))
    if samples < needed:
        samples = min(needed, 20000)
    rows = []
    closes = [c["close"] for c in candles]
    mkt = [0.0] + [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    has_open = all("open" in c for c in candles[:5])

    for name, pos in positions_by_name.items():
        cr = close_returns(candles, pos, cost)
        close_sh = ana.ann_sharpe(cr[warmup:], bpy)
        if has_open:
            dr = delayed_returns(candles, pos, cost)
            delayed_sh = ana.ann_sharpe(dr[warmup:], bpy)
        else:
            delayed_sh = None
        perm = rotation_pvalue(pos, mkt, bpy, warmup=warmup, samples=samples,
                               seed=seed)
        # the same test on the held-out tail alone: low power (few bars, less
        # time in market), so it is reported as a caveat rather than a gate
        p_oos = None
        if oos_start and len(candles) - oos_start > 120:
            p_oos = rotation_pvalue(pos, mkt, bpy, warmup=oos_start,
                                    samples=min(samples, 4000),
                                    seed=seed + 1)["p_value"]
        rows.append({
            "name": name,
            "close_sharpe": round(close_sh, 2),
            "delayed_sharpe": None if delayed_sh is None else round(delayed_sh, 2),
            "delay_cost": (None if delayed_sh is None
                           else round(close_sh - delayed_sh, 2)),
            "exposure": round(sum(1 for p in pos[warmup:] if p) /
                              max(1, len(pos) - warmup), 3),
            "p_oos": p_oos,
            **perm,
        })

    # Collapse near-identical candidates before correcting. Eleven combinations
    # that all contain the same rule are eleven copies of one test, and feeding
    # copies to Benjamini-Hochberg manufactures significance: each duplicate
    # raises the rank of the others and loosens the threshold for all of them.
    clusters = _dedupe(positions_by_name, rows, warmup)
    tested = [r for r in rows if not r.get("duplicate_of")]
    m = m_total or len(tested)
    rejected, adj = benjamini_hochberg([r["p_value"] for r in tested], alpha, m)
    for r, rej, a in zip(tested, rejected, adj):
        r["p_adjusted"], r["significant"] = a, rej
    for r in rows:
        if r.get("duplicate_of"):
            src = next(x for x in tested if x["name"] == r["duplicate_of"])
            r["p_adjusted"], r["significant"] = src["p_adjusted"], src["significant"]
    rejected = [r["significant"] for r in rows]
    adj = [r["p_adjusted"] for r in rows]
    bonferroni = alpha / max(1, m)
    for r in rows:
        r["bonferroni_ok"] = r["p_value"] <= bonferroni
        fails = []
        if r.get("duplicate_of"):
            fails.append(f"duplicate of {r['duplicate_of']}")
        if r["delayed_sharpe"] is not None and r["delayed_sharpe"] <= 0:
            fails.append("dies on next-open fills")
        if not r["significant"]:
            fails.append(f"not significant after correction "
                         f"(p_adj {r['p_adjusted']})")
        if benchmark is not None and r["close_sharpe"] <= benchmark:
            fails.append("does not beat buy & hold")
        r["fails"] = fails
        r["caveats"] = ([] if r.get("p_oos") is None or r["p_oos"] <= 0.20
                        else [f"timing not significant on the held-out tail alone "
                              f"(p_oos {r['p_oos']})"])
        if fails:
            r["verdict"] = "FAILS: " + "; ".join(fails)
        elif r["caveats"]:
            r["verdict"] = "SURVIVES (weak OOS)"
        else:
            r["verdict"] = "SURVIVES"
    rows.sort(key=lambda r: (not r["verdict"].startswith("SURVIVES"),
                             bool(r["caveats"]), r["p_adjusted"],
                             -r["close_sharpe"]))
    return {"rows": rows, "alpha": alpha, "bonferroni": round(bonferroni, 6),
            "p_floor": round(1.0 / (samples + 1), 6), "clusters": clusters,
            "independent": len(tested),
            "hypotheses": m, "shortlist": len(rows),
            "benchmark_sharpe": benchmark, "permutations": samples}


def _dedupe(positions_by_name, rows, warmup, overlap=0.90):
    """Mark candidates whose positions agree with a stronger one almost always.

    Agreement is measured on the bars where either is in the market — two rules
    that are both flat 94% of the time are not similar for that reason.
    """
    ranked = sorted(rows, key=lambda r: -abs(r["close_sharpe"]))
    kept, clusters = [], {}
    for r in ranked:
        pos = positions_by_name[r["name"]][warmup:]
        for k in kept:
            other = positions_by_name[k][warmup:]
            union = sum(1 for a, b in zip(pos, other) if a or b)
            if not union:
                continue
            agree = sum(1 for a, b in zip(pos, other) if a and b)
            if agree / union >= overlap:
                r["duplicate_of"] = k
                clusters.setdefault(k, []).append(r["name"])
                break
        else:
            kept.append(r["name"])
            clusters.setdefault(r["name"], [])
    return clusters


def print_gauntlet(res, top=20):
    print(f"\n=== EDGE VALIDATION (shortlist of {res['shortlist']} from "
          f"{res['hypotheses']} hypotheses explored, "
          f"{res['permutations']} permutations each) ===")
    print(f"  {'candidate':<38}{'Sh(cls)':>8}{'Sh(+1)':>8}{'expo':>7}"
          f"{'p':>9}{'p_adj':>8}{'p_oos':>8}  verdict")
    for r in res["rows"][:top]:
        d = "-" if r["delayed_sharpe"] is None else f"{r['delayed_sharpe']:.2f}"
        po = "-" if r.get("p_oos") is None else f"{r['p_oos']:.3f}"
        v = r["verdict"] if r["verdict"].startswith("SURVIVES") else r["verdict"][:38]
        print(f"  {r['name']:<38}{r['close_sharpe']:>8}{d:>8}"
              f"{r['exposure']:>7}{r['p_value']:>9.5f}{r['p_adjusted']:>8}"
              f"{po:>8}  {v}")
    floor = res.get("p_floor", 0)
    if floor > res["alpha"] / max(1, res["hypotheses"]):
        print(f"  NOTE: the smallest p this many permutations can report is "
              f"{floor} — raise --permutations to resolve below the correction "
              f"threshold.")
    n_ok = sum(1 for r in res["rows"] if r["verdict"].startswith("SURVIVES"))
    dupes = res["shortlist"] - res["independent"]
    print(f"  {n_ok} of {res['shortlist']} shortlisted survived "
          f"({dupes} collapsed as near-duplicates of a stronger candidate, so "
          f"{res['independent']} independent tests were corrected).")
    print(f"  Benjamini-Hochberg at alpha={res['alpha']} over "
          f"{res['hypotheses']} explored hypotheses; Bonferroni would need "
          f"p <= {res['bonferroni']}.")
    if res.get("benchmark_sharpe") is not None:
        print(f"  Buy & hold Sharpe on the same bars: {res['benchmark_sharpe']:.2f} "
              "— candidates below it are beta, whatever their p-value says.")
