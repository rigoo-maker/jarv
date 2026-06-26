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


def run(candles, *, enter_score=35, exit_score=-10, fee_bps=10.0,
        slippage_bps=2.0, start_equity=1000.0, compound=True, warmup=60):
    """Long/flat backtest of the scoring engine over `candles`.

    fee_bps: round-trip-ish per-side taker fee in basis points (Binance ~10bps
             taker; lower with BNB/maker). slippage_bps: assumed adverse fill.
    compound: size each entry as full current equity (geometric compounding).
    """
    equity = start_equity
    in_pos = False
    entry_px = 0.0
    entry_eq = 0.0
    entry_bar = 0
    fees_paid = 0.0
    cost = (fee_bps + slippage_bps) / 1e4   # per side
    curve = [equity]
    trades = []

    for i in range(warmup, len(candles)):
        window = candles[: i + 1]
        snap = indicators.compute_all(window)["latest"]
        score = score_snapshot(snap)["score"]
        px = window[-1]["close"]

        if not in_pos and score >= enter_score:
            in_pos = True
            entry_px = px * (1 + cost)        # pay fee+slippage on entry
            entry_eq = equity if compound else start_equity
            entry_bar = i
            fees_paid += entry_eq * cost
        elif in_pos and score <= exit_score:
            exit_px = px * (1 - cost)         # pay fee+slippage on exit
            gross_ret = exit_px / entry_px - 1
            pnl = entry_eq * gross_ret
            equity += pnl
            fees_paid += entry_eq * cost
            trades.append({"entry": entry_px, "exit": exit_px, "ret": gross_ret,
                           "pnl": pnl, "bars": i - entry_bar})
            in_pos = False
        # mark-to-market equity curve
        if in_pos:
            mtm = entry_eq * (px / entry_px - 1)
            curve.append((equity if not compound else equity) + 0)  # realized only
        else:
            curve.append(equity)

    return BacktestResult(curve, trades, fees_paid,
                          {"enter_score": enter_score, "exit_score": exit_score,
                           "fee_bps": fee_bps, "slippage_bps": slippage_bps,
                           "compound": compound})


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
