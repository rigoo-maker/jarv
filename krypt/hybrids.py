"""Hybrid strategies: combining rules instead of picking one.

Single rules on one instrument are thin. Combining them is the obvious next
move, and it is also where people quietly fool themselves, because there are
many more combinations than rules and at least one of them always looks great.

Five combination TYPES, because they behave differently and the difference is
the point:

  * **AND (confluence)** — long only when every member is long. Fewer trades,
    lower exposure, higher hit rate if the members are genuinely independent.
    This is the one that most often improves risk-adjusted return.
  * **OR (union)** — long when any member is long. More exposure, more trades;
    usually drifts toward buy-and-hold, which is exactly what to check for.
  * **Vote (k of n)** — long when at least k members agree. The dial between
    AND and OR.
  * **Regime switch** — one rule above the 200-day line, another below. Uses the
    regime map's finding rather than averaging over it.
  * **Portfolio** — split capital equally and rebalance daily. This does NOT
    combine signals; it combines equity curves, so it only helps when the
    members are uncorrelated. Measured from blended per-bar net returns rather
    than a fractional position, so the fee model stays honest.

Everything returns the same shape as a single strategy, so hybrids go through
the identical backtest, out-of-sample split and prop evaluation — a hybrid gets
no easier grading than the rules it is made of.
"""

from __future__ import annotations

import math

from . import backtest as bt
from . import analytics as ana


# --------------------------------------------------------------- combinators

def combine_and(series):
    n = len(series[0])
    return [1 if all(s[i] > 0 for s in series) else 0 for i in range(n)]


def combine_or(series):
    n = len(series[0])
    return [1 if any(s[i] > 0 for s in series) else 0 for i in range(n)]


def combine_vote(series, k):
    n = len(series[0])
    return [1 if sum(1 for s in series if s[i] > 0) >= k else 0 for i in range(n)]


def combine_switch(cache, bull_series, bear_series):
    """`bull_series` while price is above its 200-day line, `bear_series` below."""
    close, ma = cache["close"], cache.get("sma200")
    n = len(close)
    if ma is None:
        return list(bull_series)
    out = []
    for i in range(n):
        if ma[i] is None:
            out.append(0)
        else:
            out.append(bull_series[i] if close[i] > ma[i] else bear_series[i])
    return out


MODES = {
    "and": lambda cache, series: combine_and(series),
    "or": lambda cache, series: combine_or(series),
    "switch": lambda cache, series: combine_switch(cache, series[0], series[1]),
}


# ---------------------------------------------------------------- evaluation

def evaluate_positions(cache, pos, *, bpy, warmup=300, split=None, fee_bps=1.0,
                       slippage_bps=2.0, allow_short=False):
    """Backtest an arbitrary position series and return annualized stats."""
    def fn(_c, i, _p=pos):
        return _p[i]

    def run(a, b):
        sub = ana.slice_cache(cache, a, b)
        res = bt.run_signal(sub, lambda c, i, _p=pos, _a=a: _p[_a + i],
                            fee_bps=fee_bps, slippage_bps=slippage_bps,
                            warmup=1, allow_short=allow_short)
        st = res.stats()
        eq = res.equity_curve
        rr = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
        st["ann_sharpe"] = round(ana.ann_sharpe(rr, bpy), 2)
        return st

    n = len(cache["close"])
    split = split or int(warmup + (n - warmup) * 0.7)
    full, is_, oos = run(warmup, n), run(warmup, split), run(split, n)
    exposure = sum(1 for p in pos[warmup:] if p != 0) / max(1, n - warmup)
    return {
        "full_sharpe": full["ann_sharpe"], "is_sharpe": is_["ann_sharpe"],
        "oos_sharpe": oos["ann_sharpe"],
        "return_pct": full["total_return_pct"],
        "oos_return_pct": oos["total_return_pct"],
        "max_dd_pct": full["max_drawdown_pct"], "trades": full["trades"],
        "win_rate_pct": full["win_rate_pct"], "exposure": round(exposure, 3),
    }


def portfolio_stats(ret_series, bpy, warmup=300):
    """Equal-weight daily-rebalanced blend of per-bar NET return series."""
    n = len(ret_series[0])
    w = 1.0 / len(ret_series)
    blended = [sum(s[i] for s in ret_series) * w for i in range(n)]
    eq, curve = 1.0, [1.0]
    peak, mdd = 1.0, 0.0
    for i in range(warmup, n):
        eq *= (1 + blended[i])
        curve.append(eq)
        peak = max(peak, eq)
        mdd = max(mdd, (peak - eq) / peak)
    seg = blended[warmup:]
    return {"return_pct": round((eq - 1) * 100, 2),
            "full_sharpe": round(ana.ann_sharpe(seg, bpy), 2),
            "max_dd_pct": round(mdd * 100, 2),
            "curve": curve}


# -------------------------------------------------------------------- search

def search_pairs(cache, positions, *, bpy, warmup=300, split=None, fee_bps=1.0,
                 slippage_bps=2.0, modes=("and", "or", "switch"), min_trades=10):
    """Every ordered/unordered pair under every mode, evaluated identically.

    Returns (rows, matrices). `rows` is the ranked list; `matrices` holds one
    strategy x strategy grid per symmetric mode for the heatmap. AND and OR are
    symmetric so half the grid is redundant, but a full square reads faster than
    a triangle and costs nothing.
    """
    names = list(positions)
    rows, matrices = [], {}
    for mode in modes:
        grid = [[None] * len(names) for _ in names]
        for a, na in enumerate(names):
            for b, nb in enumerate(names):
                if a == b:
                    continue
                if mode != "switch" and b < a:
                    grid[a][b] = grid[b][a]
                    continue
                pos = MODES[mode](cache, [positions[na], positions[nb]])
                if sum(pos[warmup:]) == 0:
                    continue
                st = evaluate_positions(cache, pos, bpy=bpy, warmup=warmup,
                                        split=split, fee_bps=fee_bps,
                                        slippage_bps=slippage_bps)
                grid[a][b] = st["oos_sharpe"]
                if st["trades"] >= min_trades:
                    rows.append({"name": f"{na} {mode.upper()} {nb}", "mode": mode,
                                 "members": [na, nb], "positions": pos, **st})
        matrices[mode] = {"names": names, "matrix": grid}
    rows.sort(key=lambda r: (r["oos_sharpe"], r["full_sharpe"]), reverse=True)
    return rows, matrices


def search_votes(cache, positions, *, bpy, warmup=300, split=None, fee_bps=1.0,
                 slippage_bps=2.0, ks=(2, 3, 4)):
    """k-of-n votes across the whole library — the dial between AND and OR."""
    names = list(positions)
    series = [positions[n] for n in names]
    out = []
    for k in ks:
        pos = combine_vote(series, k)
        if sum(pos[warmup:]) == 0:
            continue
        st = evaluate_positions(cache, pos, bpy=bpy, warmup=warmup, split=split,
                                fee_bps=fee_bps, slippage_bps=slippage_bps)
        out.append({"name": f"vote {k}-of-{len(names)}", "mode": "vote",
                    "members": names, "positions": pos, **st})
    return out


def build_portfolios(cache, positions, ranked_names, *, bpy, warmup=300,
                     cost=0.0003, sizes=(2, 3, 5)):
    """Equal-weight portfolios of the top-N rules by whatever ranked them."""
    closes = cache["close"]
    rets = {n: ana.bar_returns(positions[n], closes, cost) for n in positions}
    out = []
    for k in sizes:
        members = ranked_names[:k]
        if len(members) < k:
            continue
        st = portfolio_stats([rets[m] for m in members], bpy, warmup)
        st.pop("curve", None)
        out.append({"name": f"portfolio of top {k}", "mode": "portfolio",
                    "members": members, **st})
    return out


def print_hybrids(rows, benchmark=None, top=15):
    print(f"\n=== HYBRID COMBINATIONS (ranked by OUT-OF-SAMPLE Sharpe) ===")
    print(f"  {'combination':<44}{'fullSh':>8}{'oosSh':>8}{'ret%':>9}"
          f"{'maxDD%':>8}{'expo':>7}{'trades':>8}")
    for r in rows[:top]:
        print(f"  {r['name']:<44}{r['full_sharpe']:>8}{r['oos_sharpe']:>8}"
              f"{r['return_pct']:>9}{r['max_dd_pct']:>8}"
              f"{r.get('exposure', 0):>7}{r.get('trades', 0):>8}")
    if benchmark:
        print(f"  buy & hold on the same bars: Sharpe {benchmark['full_sharpe']}, "
              f"OOS Sharpe {benchmark['oos_sharpe']}, "
              f"return {benchmark['return_pct']}%, maxDD {benchmark['max_dd_pct']}%")
    print("  NOTE: every pair x mode is a separate hypothesis. Ranking hundreds of "
          "them\n  guarantees a good-looking top row — see the significance table "
          "for whether\n  it survives a multiple-testing correction.")
