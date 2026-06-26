"""Realistic backtester — measures whether a strategy has edge AFTER costs.

The whole point: a strategy that looks great with zero fees usually dies once
you subtract taker fees + slippage. This engine subtracts them, compounds
equity (so winners grow position size), and reports the stats that actually
matter (Sharpe, max drawdown, profit factor) — not just "final number went up".

It is still a simplification (bar-close fills, no partial fills, no funding,
no latency). Treat a good backtest as NECESSARY but NOT SUFFICIENT. Overfitting
is the default outcome; out-of-sample test before believing anything.
"""

from __future__ import annotations

import math

from . import indicators
from .scoring import score_snapshot


class BacktestResult:
    def __init__(self, equity_curve, trades, fees_paid, params):
        self.equity_curve = equity_curve
        self.trades = trades            # list of {entry, exit, ret, pnl, bars}
        self.fees_paid = fees_paid
        self.params = params

    def stats(self):
        eq = self.equity_curve
        start, end = eq[0], eq[-1]
        rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq))]
        # max drawdown
        peak, mdd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            mdd = max(mdd, (peak - v) / peak)
        # Sharpe (per-bar, annualized rough); guard tiny std
        if rets:
            mu = sum(rets) / len(rets)
            var = sum((r - mu) ** 2 for r in rets) / len(rets)
            sd = math.sqrt(var)
            sharpe = (mu / sd * math.sqrt(len(rets))) if sd > 1e-12 else 0.0
        else:
            mu = sharpe = 0.0
        wins = [t for t in self.trades if t["pnl"] > 0]
        losses = [t for t in self.trades if t["pnl"] <= 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in losses)
        pf = (gross_win / gross_loss) if gross_loss > 1e-9 else float("inf")
        return {
            "start_equity": round(start, 2),
            "final_equity": round(end, 2),
            "total_return_pct": round((end / start - 1) * 100, 2),
            "trades": len(self.trades),
            "win_rate_pct": round(100 * len(wins) / len(self.trades), 1) if self.trades else 0.0,
            "profit_factor": round(pf, 2) if pf != float("inf") else None,
            "max_drawdown_pct": round(mdd * 100, 2),
            "sharpe": round(sharpe, 2),
            "fees_paid": round(self.fees_paid, 2),
            "avg_trade_pct": round(100 * sum(t["ret"] for t in self.trades) / len(self.trades), 3) if self.trades else 0.0,
        }


def run(candles, *, fee_bps=10.0, slippage_bps=2.0, start_equity=1000.0,
        compound=True, warmup=60):
    """Long/flat backtest of the confluence scoring engine (no leverage, no short).
    Thin wrapper over the fast precomputed path."""
    from . import strats
    cache = strats.precompute(candles)
    return run_signal(cache, strats.confluence, leverage=1.0, fee_bps=fee_bps,
                      slippage_bps=slippage_bps, start_equity=start_equity,
                      compound=compound, warmup=warmup, allow_short=False)


def run_signal(cache, signal_fn, *, leverage=1.0, fee_bps=10.0, slippage_bps=2.0,
               start_equity=1000.0, compound=True, warmup=60, allow_short=True):
    """Backtest a signal strategy (+1/0/-1) over a precomputed `cache` (from
    strats.precompute) with LEVERAGE and a liquidation model.

    Liquidation is the honest part: a leveraged position is wiped when the adverse
    move approaches 1/leverage (you can't lose more than your margin — you lose
    ALL of it). This is why high leverage on a thin edge => ruin. The backtest
    shows it instead of hiding it.
    """
    closes = cache["close"]
    n = len(closes)
    equity = start_equity
    pos = 0                 # -1, 0, +1
    entry = 0.0
    cost = (fee_bps + slippage_bps) / 1e4
    curve = [equity]
    trades = []
    fees_paid = 0.0
    liquidations = 0
    maint = 0.95 / max(leverage, 1.0)   # adverse fraction that liquidates

    def close(px, i):
        nonlocal equity, pos, fees_paid
        ret = ((px - entry) / entry) * pos
        base = equity if compound else start_equity
        pnl = base * leverage * ret
        equity += pnl
        f = base * leverage * cost
        equity -= f
        fees_paid += f
        trades.append({"entry": entry, "exit": px, "ret": ret, "pnl": pnl,
                       "bars": i, "lev": leverage})

    for i in range(warmup, n):
        px = closes[i]

        # liquidation check on the open leveraged position
        if pos != 0 and leverage > 1.0:
            adverse = ((entry - px) / entry) if pos > 0 else ((px - entry) / entry)
            if adverse >= maint:
                base = equity if compound else start_equity
                equity -= base                      # margin wiped
                equity = max(equity, 0.0)
                trades.append({"entry": entry, "exit": px, "ret": -1.0,
                               "pnl": -base, "bars": i, "lev": leverage,
                               "liquidated": True})
                liquidations += 1
                pos = 0
                curve.append(equity)
                if equity <= start_equity * 0.01:
                    break
                continue

        target = signal_fn(cache, i)
        if not allow_short and target < 0:
            target = 0
        if target != pos:
            if pos != 0:
                close(px, i)
            if target != 0 and equity > 0:
                entry = px
                base = equity if compound else start_equity
                f = base * leverage * cost
                equity -= f
                fees_paid += f
            pos = target
        curve.append(equity)

    if pos != 0 and equity > 0:
        close(closes[-1], n)

    res = BacktestResult(curve, trades, fees_paid,
                         {"leverage": leverage, "fee_bps": fee_bps,
                          "slippage_bps": slippage_bps, "compound": compound,
                          "allow_short": allow_short})
    res.liquidations = liquidations
    return res


def compare(candles, strat_registry, *, leverage=1.0, **kw):
    """Run every strategy, return a leaderboard ranked by Sharpe then return."""
    from . import strats
    cache = strats.precompute(candles)      # compute indicators ONCE for all
    rows = []
    for name, fn in strat_registry.items():
        try:
            r = run_signal(cache, fn, leverage=leverage, **kw)
            s = r.stats()
            s["strategy"] = name
            s["liquidations"] = getattr(r, "liquidations", 0)
            rows.append(s)
        except Exception as e:
            rows.append({"strategy": name, "error": str(e), "sharpe": -99,
                         "total_return_pct": -100})
    rows.sort(key=lambda x: (x.get("sharpe", -99), x.get("total_return_pct", -100)),
              reverse=True)
    return rows


def print_leaderboard(symbol, interval, rows, leverage, n_candles):
    print(f"\n=== Strategy leaderboard {symbol} {interval} "
          f"({n_candles} candles, {leverage}x leverage) ===")
    print(f"  {'strategy':<18}{'return%':>9}{'sharpe':>8}{'win%':>7}"
          f"{'PF':>7}{'maxDD%':>8}{'trades':>8}{'liq':>5}")
    for r in rows:
        if "error" in r:
            print(f"  {r['strategy']:<18}  ERROR: {r['error'][:40]}")
            continue
        pf = r["profit_factor"]
        print(f"  {r['strategy']:<18}{r['total_return_pct']:>9}{r['sharpe']:>8}"
              f"{r['win_rate_pct']:>7}{(pf if pf is not None else 0):>7}"
              f"{r['max_drawdown_pct']:>8}{r['trades']:>8}{r.get('liquidations',0):>5}")
    print("  ranked by Sharpe. 'liq' = liquidations (>0 means leverage blew it up).")
    print("  IN-SAMPLE ONLY — the top row is a hypothesis, not a deployable edge.")


def print_report(symbol, interval, result, n_candles):
    s = result.stats()
    print(f"\n=== Backtest {symbol} {interval}  ({n_candles} candles) ===")
    print(f"  start equity   : ${s['start_equity']:,}")
    print(f"  final equity   : ${s['final_equity']:,}   ({s['total_return_pct']:+}% )")
    print(f"  trades         : {s['trades']}   win rate {s['win_rate_pct']}%")
    print(f"  profit factor  : {s['profit_factor']}")
    print(f"  avg trade      : {s['avg_trade_pct']}%")
    print(f"  max drawdown   : {s['max_drawdown_pct']}%")
    print(f"  Sharpe (rough) : {s['sharpe']}")
    print(f"  fees+slippage  : ${s['fees_paid']:,}  (bps: fee {result.params['fee_bps']}, "
          f"slip {result.params['slippage_bps']})")
    print(f"  compounding    : {'ON' if result.params['compound'] else 'OFF'}")
    edge = (s['profit_factor'] or 0)
    verdict = ("LIKELY NO EDGE after costs" if (s['total_return_pct'] <= 0 or (edge and edge < 1.1))
               else "shows edge in-sample — now test OUT-of-sample before trusting it")
    print(f"  >> {verdict}")
    print("  (bar-close fills, no funding/latency — backtest = necessary, not sufficient)")
