"""Evaluation-aware backtester.

A normal backtester answers "did this make money". That is the wrong question
for a funded-account evaluation. The right question is "does this PASS, and how
often", because a strategy with a good expectancy that occasionally draws down
$2,001 has a pass rate of zero no matter how good its Sharpe is.

So this runs the account rules bar by bar and reports PASS / FAIL / BREACH,
and `monte_carlo` repeats the whole evaluation thousands of times to turn a
single lucky path into a distribution.

Fill assumptions, all deliberately pessimistic:
  * entry fills at the signal bar's close, plus slippage against you
  * if a bar's range contains BOTH stop and target, the STOP is assumed first
  * commissions charged round-turn, per contract, on every trade
  * open positions are flattened at session end (no overnight risk, matching
    most evaluation rules)
"""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field

from .rules import AccountState, AccountSpec, Breach


@dataclass
class Trade:
    day: str
    side: str
    qty: int
    entry: float
    exit: float
    stop: float
    target: float
    pnl: float                 # net, after commission
    gross: float
    commission: float
    bars_held: int
    exit_reason: str
    reason: str = ""


@dataclass
class EvalResult:
    passed: bool
    breached: str
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)
    days: int = 0
    final_state: dict = field(default_factory=dict)

    def summary(self) -> dict:
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        peak, max_dd = None, 0.0
        for e in self.equity_curve:
            peak = e if peak is None else max(peak, e)
            max_dd = max(max_dd, peak - e)
        return {
            "passed": self.passed,
            "breached": self.breached,
            "trades": len(self.trades),
            "win_rate_pct": round(len(wins) / len(self.trades) * 100, 1)
                            if self.trades else None,
            "avg_win": round(gross_win / len(wins), 2) if wins else None,
            "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "net_pnl": round(sum(t.pnl for t in self.trades), 2),
            "commissions": round(sum(t.commission for t in self.trades), 2),
            "max_drawdown": round(max_dd, 2),
            "trading_days": self.days,
        }


def _day_of(bar, tz_offset_min=0) -> str:
    ms = bar["time"] + tz_offset_min * 60000
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _minute_of_day(bar, tz_offset_min=0) -> int:
    return int(((bar["time"] // 60000) + tz_offset_min) % 1440)


def run(bars, strategy, instrument, spec: AccountSpec, *,
        risk_fraction=0.25, slippage_ticks=1, tz_offset_min=0,
        flatten_minute=955, max_trades_per_day=5, strategy_kwargs=None,
        stop_when_passed=True):
    """Walk bars once, trading the strategy under full account rules."""
    kw = strategy_kwargs or {}
    state = AccountState(spec)
    result = EvalResult(passed=False, breached=None)

    position = None            # dict: side, qty, entry, stop, target, bar, reason
    day = None
    trades_today = 0

    def close_position(price, reason, idx):
        nonlocal position
        p = position
        gross = instrument.pnl(p["entry"], price, p["qty"], p["side"] == "long")
        comm = instrument.commission_rt * p["qty"]
        net = gross - comm
        t = Trade(day=day, side=p["side"], qty=p["qty"], entry=p["entry"],
                  exit=price, stop=p["stop"], target=p["target"], pnl=net,
                  gross=gross, commission=comm, bars_held=idx - p["bar"],
                  exit_reason=reason, reason=p["reason"])
        position = None
        result.trades.append(t)
        state.on_trade_closed(net)
        result.equity_curve.append(state.equity)
        return t

    try:
        for i, bar in enumerate(bars):
            d = _day_of(bar, tz_offset_min)
            if d != day:
                if position is not None:
                    close_position(bars[i - 1]["close"], "session_flat", i - 1)
                day = d
                trades_today = 0
                state.start_day(d)
                result.days += 1

            minute = _minute_of_day(bar, tz_offset_min)

            # --- manage the open position first -------------------------
            if position is not None:
                p = position
                hit_stop = (bar["low"] <= p["stop"] if p["side"] == "long"
                            else bar["high"] >= p["stop"])
                hit_tgt = (p["target"] is not None and
                           (bar["high"] >= p["target"] if p["side"] == "long"
                            else bar["low"] <= p["target"]))
                if hit_stop:
                    # Pessimistic: stop wins any same-bar ambiguity.
                    slip = slippage_ticks * instrument.tick_size
                    fill = p["stop"] - slip if p["side"] == "long" else p["stop"] + slip
                    close_position(fill, "stop", i)
                elif hit_tgt:
                    close_position(p["target"], "target", i)
                elif minute >= flatten_minute:
                    close_position(bar["close"], "session_flat", i)
                else:
                    mtm = instrument.pnl(p["entry"], bar["close"], p["qty"],
                                         p["side"] == "long") - \
                          instrument.commission_rt * p["qty"]
                    state.mark(mtm)

            # --- look for a new entry ------------------------------------
            if (position is None and minute < flatten_minute
                    and trades_today < max_trades_per_day
                    and not (stop_when_passed and state.passed)):
                sig = strategy(bars, i, **kw)
                if sig is not None and sig.stop_distance > 0:
                    ticks = sig.stop_ticks(instrument)
                    qty = state.max_contracts_for_risk(instrument, ticks,
                                                       risk_fraction)
                    if qty > 0:
                        slip = slippage_ticks * instrument.tick_size
                        entry = (sig.entry + slip if sig.side == "long"
                                 else sig.entry - slip)
                        position = {"side": sig.side, "qty": qty, "entry": entry,
                                    "stop": sig.stop, "target": sig.target,
                                    "bar": i, "reason": sig.reason}
                        trades_today += 1

            if state.passed and stop_when_passed:
                break
    except Breach as e:
        result.breached = str(e)

    if position is not None and not result.breached:
        close_position(bars[-1]["close"], "end_of_data", len(bars) - 1)

    result.passed = state.passed
    result.final_state = state.snapshot()
    return result


# ---------------------------------------------------------------------------
# Monte Carlo over the evaluation itself
# ---------------------------------------------------------------------------
def monte_carlo(spec: AccountSpec, *, win_rate, avg_win, avg_loss,
                trades_per_day=3, max_days=60, sims=5000, seed=7,
                commission=1.34, contracts=1):
    """How often does a trader with THESE statistics pass this evaluation?

    Takes the edge as given (win rate, average win, average loss in USD per
    contract) and answers the only question that matters: what fraction of
    runs reach the profit target before breaching a limit.

    This is where most evaluation planning falls apart. A 50% win rate at 2:1
    reward:risk feels safe and still fails a meaningful share of the time,
    because the trailing drawdown converts an ordinary losing streak into a
    terminal event.
    """
    rng = random.Random(seed)
    outcomes = {"passed": 0, "breached_mll": 0, "breached_dll": 0,
                "timeout": 0, "consistency_blocked": 0}
    days_to_pass, final_profits = [], []

    for _ in range(sims):
        state = AccountState(spec)
        result = None
        for d in range(max_days):
            day = f"d{d:03d}"
            try:
                state.start_day(day)
                for _ in range(trades_per_day):
                    if rng.random() < win_rate:
                        pnl = avg_win * contracts - commission * contracts
                    else:
                        pnl = -avg_loss * contracts - commission * contracts
                    state.on_trade_closed(pnl)
                    if state.passed:
                        break
            except Breach as e:
                result = "breached_mll" if "MAX LOSS" in str(e) else "breached_dll"
                break
            if state.passed:
                result = "passed"
                days_to_pass.append(d + 1)
                break
            if state.profit >= spec.profit_target and state.consistency_violation():
                result = "consistency_blocked"
                break
        if result is None:
            result = "timeout"
        outcomes[result] += 1
        final_profits.append(state.profit)

    n = float(sims)
    return {
        "sims": sims,
        "pass_rate_pct": round(outcomes["passed"] / n * 100, 2),
        "breach_mll_pct": round(outcomes["breached_mll"] / n * 100, 2),
        "breach_dll_pct": round(outcomes["breached_dll"] / n * 100, 2),
        "consistency_blocked_pct": round(outcomes["consistency_blocked"] / n * 100, 2),
        "timeout_pct": round(outcomes["timeout"] / n * 100, 2),
        "median_days_to_pass": (sorted(days_to_pass)[len(days_to_pass) // 2]
                                if days_to_pass else None),
        "expectancy_per_trade": round(
            win_rate * avg_win - (1 - win_rate) * avg_loss - commission, 2),
        "mean_final_profit": round(sum(final_profits) / n, 2),
    }
