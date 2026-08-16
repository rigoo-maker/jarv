"""Prop-firm evaluation rules (Apex Trader Funding, Topstep) as a testable engine.

A prop account is not a trading account with a smaller balance — it is a
different game with different failure modes, and a strategy that is excellent in
a normal backtest can be structurally unable to pass:

  * **Trailing drawdown.** The kill line follows your equity UP and never comes
    back down. A strategy that runs +$4,000 then gives back $2,600 is dead at
    Apex 50k even though it is up $1,400 on the day.
  * **It trails on UNREALIZED equity (Apex).** Your open profit raises the line.
    Letting a winner run and then giving it back is the classic blow-up.
  * **Daily loss limit (Topstep).** One bad session ends the account, regardless
    of the equity curve's shape.
  * **No overnight positions.** Every position must be flat before the session
    close. Any swing strategy is disqualified before Sharpe is even discussed.
  * **Consistency.** One monster day can disqualify a payout even when the total
    is fine.

So "does this strategy have an edge" and "would this strategy pass an evaluation"
are different questions, and this module answers the second one. It simulates the
rules bar by bar and reports PASS / FAIL(reason), then repeats the simulation from
many different start dates — because passing once from a lucky start is not a
result, and the distribution of outcomes across start dates is.

RULE NUMBERS CHANGE. Every preset below is a starting point with `as_of` marked
and every field overridable (`--rules-json`). Check the firm's current rulebook
before you trust a PASS from this file; a stale drawdown number here is a real
account there.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

# ---------------------------------------------------------------- instruments

@dataclass
class Contract:
    """Futures contract spec — what one point is worth and what a turn costs."""
    symbol: str
    point_value: float          # $ per 1.00 of price movement, per contract
    tick_size: float
    commission_rt: float        # $ per contract, round turn (fees + clearing)


CONTRACTS = {
    # micros are what evaluation accounts are actually traded on
    "MNQ": Contract("MNQ", 2.0, 0.25, 1.40),
    "MES": Contract("MES", 5.0, 0.25, 1.40),
    "M2K": Contract("M2K", 5.0, 0.10, 1.40),
    "MYM": Contract("MYM", 0.50, 1.0, 1.40),
    "MGC": Contract("MGC", 10.0, 0.10, 1.60),
    "MCL": Contract("MCL", 100.0, 0.01, 1.60),
    "NQ": Contract("NQ", 20.0, 0.25, 4.00),
    "ES": Contract("ES", 50.0, 0.25, 4.00),
    "RTY": Contract("RTY", 50.0, 0.10, 4.00),
    "YM": Contract("YM", 5.0, 1.0, 4.00),
    "GC": Contract("GC", 100.0, 0.10, 4.50),
    "CL": Contract("CL", 1000.0, 0.01, 4.50),
}


# --------------------------------------------------------------------- rules

@dataclass
class PropRules:
    """One evaluation account's rulebook."""
    firm: str
    label: str
    starting_balance: float
    profit_target: float
    max_drawdown: float                  # trailing threshold / max loss limit
    trail_mode: str = "intraday"         # "intraday" (on unrealized) | "eod"
    trail_lock: str = "none"             # "none" | "start" | "start_plus_100"
    daily_loss_limit: float | None = None
    max_contracts: int = 10              # in the instrument's full-size terms
    min_trading_days: int = 0
    min_winning_days: int = 0
    winning_day_min: float = 0.0         # $ that makes a day "winning"
    consistency_pct: float | None = None  # max share of profit from one day
    allow_overnight: bool = False
    as_of: str = "unverified — check the firm's current rulebook"
    notes: str = ""

    def to_json(self):
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


def _apex(size_label, balance, target, dd, contracts):
    return PropRules(
        firm="apex", label=f"Apex {size_label}", starting_balance=balance,
        profit_target=target, max_drawdown=dd,
        trail_mode="intraday",          # trails on unrealized equity highs
        trail_lock="start_plus_100",    # stops trailing at start + $100
        daily_loss_limit=None,          # Apex has no daily loss limit
        max_contracts=contracts,
        min_trading_days=7, consistency_pct=0.30,
        allow_overnight=False,
        as_of="2025 rulebook — VERIFY",
        notes="Threshold trails intraday on unrealized profit and locks once it "
              "reaches start + $100. 30% consistency applies to payouts. Half "
              "contract size until the threshold locks.")


def _topstep(size_label, balance, target, mll, dll, contracts):
    return PropRules(
        firm="topstep", label=f"Topstep {size_label}", starting_balance=balance,
        profit_target=target, max_drawdown=mll,
        trail_mode="eod",               # MLL trails end-of-day balance only
        trail_lock="start",             # locks at the starting balance
        daily_loss_limit=dll,
        max_contracts=contracts,
        min_winning_days=2, winning_day_min=200.0,
        consistency_pct=0.50,
        allow_overnight=False,
        as_of="2025 Trading Combine rules — VERIFY",
        notes="MLL trails on END-OF-DAY balance (not intraday) and locks at the "
              "starting balance. Daily Loss Limit is intraday, measured against "
              "the start-of-day balance, and counts unrealized P&L.")


PRESETS = {
    "apex-25k": _apex("25k", 25_000, 1_500, 1_500, 4),
    "apex-50k": _apex("50k", 50_000, 3_000, 2_500, 10),
    "apex-75k": _apex("75k", 75_000, 4_250, 2_750, 12),
    "apex-100k": _apex("100k", 100_000, 6_000, 3_000, 14),
    "apex-150k": _apex("150k", 150_000, 9_000, 5_000, 17),
    "apex-250k": _apex("250k", 250_000, 15_000, 6_500, 27),
    "apex-300k": _apex("300k", 300_000, 20_000, 7_500, 35),
    "topstep-50k": _topstep("50k", 50_000, 3_000, 2_000, 1_000, 5),
    "topstep-100k": _topstep("100k", 100_000, 6_000, 3_000, 2_000, 10),
    "topstep-150k": _topstep("150k", 150_000, 9_000, 4_500, 3_000, 15),
}


def get_rules(name):
    key = name.lower().replace("_", "-")
    if key in PRESETS:
        return PRESETS[key]
    raise KeyError(f"unknown preset '{name}'. choices: {', '.join(sorted(PRESETS))}")


def load_rules(spec):
    """Preset name, or a path to a JSON file overriding any field."""
    if spec and spec.endswith(".json"):
        with open(spec) as f:
            return PropRules.from_dict(json.load(f))
    return get_rules(spec)


# ----------------------------------------------------------------- evaluation

FAIL_TRAILING = "trailing drawdown breached"
FAIL_DAILY = "daily loss limit breached"
FAIL_OVERNIGHT = "held a position overnight"
FAIL_TIME = "ran out of data before hitting the target"
FAIL_CONSISTENCY = "consistency rule (one day too large a share of profit)"
FAIL_DAYS = "profit target hit before the minimum-days requirement"


def _day_of(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def evaluate(bars, positions, rules, contract, qty=1, *, start=0, end=None,
             worst_case=True, enforce_overnight=True, notional=None):
    """Walk the rules bar by bar from `start` and return the outcome.

    `positions` is the strategy's per-bar position (+1/0/-1) — the same series
    the backtester uses, so the evaluation is of the exact strategy that was
    measured, not a re-implementation.

    `notional` switches P&L from points to PERCENT of a stated contract notional.
    Use it when the price series is a PROXY rather than the contract itself: a $70
    stock priced at $2 a point produces dollar moves three orders of magnitude too
    small, so the evaluation would "pass" by never risking anything. With
    notional set, a 1% move on one MNQ (~$40,000 notional) is ~$400, which is the
    number the rules were written against. It is still a proxy — the real contract
    has its own gaps, session and volatility.

    `worst_case=True` uses each bar's adverse extreme (low for longs, high for
    shorts) when testing breaches, and its favourable extreme when raising the
    trailing peak. That is the honest reading of an OHLC bar: the firm's rules
    are enforced tick by tick, so a bar that closed fine can still have breached
    inside itself. On daily bars this is the difference between a simulation you
    can trust and one that flatters you.
    """
    end = len(bars) if end is None else end
    if end - start < 2:
        return {"result": "insufficient data", "days": 0}

    bal = rules.starting_balance          # realized balance
    peak = bal                            # trailing peak (equity or EOD balance)
    day_start_bal = bal
    cur_day = _day_of(bars[start]["time"])
    day_pnl, day_pnls, trading_days = 0.0, [], 0
    traded_today = False
    overnight_holds = 0
    threshold_of = _threshold_fn(rules)
    prev_pos = 0
    log = []

    for i in range(start + 1, end):
        b, pb = bars[i], bars[i - 1]
        pos = positions[i - 1] * qty      # position held INTO this bar

        # dollars per contract per unit of price move: points x point value, or
        # percent of a stated notional when the series is a proxy
        if notional:
            def dollars(px, _p0=pb["close"]):
                return (px / _p0 - 1) * notional if _p0 else 0.0
        else:
            def dollars(px, _p0=pb["close"], _pv=contract.point_value):
                return (px - _p0) * _pv

        move_close = dollars(b["close"]) * pos
        if worst_case and pos:
            adverse = b["low"] if pos > 0 else b["high"]
            favourable = b["high"] if pos > 0 else b["low"]
            move_worst = dollars(adverse) * pos
            move_best = dollars(favourable) * pos
        else:
            move_worst = move_best = move_close

        # commissions when size changes (half a round turn per side)
        turn = abs(positions[i] * qty - pos)
        fees = turn * contract.commission_rt / 2.0
        if turn:
            traded_today = True

        equity_close = bal + move_close - fees
        equity_worst = bal + move_worst - fees
        equity_best = bal + move_best - fees

        # ---- trailing peak: Apex raises it on unrealized highs, Topstep only EOD
        if rules.trail_mode == "intraday":
            peak = max(peak, equity_best)
        thr = threshold_of(peak)

        # ---- breach checks, on the adverse extreme
        if equity_worst <= thr:
            return _fail(FAIL_TRAILING, i, bars, start, bal, day_pnls, trading_days,
                         rules, extra={"threshold": round(thr, 2),
                                       "equity_at_breach": round(equity_worst, 2)})
        if rules.daily_loss_limit is not None:
            if (equity_worst - day_start_bal) <= -rules.daily_loss_limit:
                return _fail(FAIL_DAILY, i, bars, start, bal, day_pnls, trading_days,
                             rules, extra={"day_loss": round(equity_worst - day_start_bal, 2)})

        bal = equity_close
        day_pnl = bal - day_start_bal

        # ---- day boundary
        d = _day_of(b["time"])
        if d != cur_day:
            if positions[i] != 0 and not rules.allow_overnight:
                overnight_holds += 1
                if enforce_overnight:
                    return _fail(FAIL_OVERNIGHT, i, bars, start, bal, day_pnls,
                                 trading_days, rules,
                                 extra={"note": "position was open across the "
                                                "session close"})
            if traded_today or day_pnl:
                day_pnls.append(day_pnl)
                trading_days += 1
            if rules.trail_mode == "eod":
                peak = max(peak, bal)
            cur_day, day_start_bal, traded_today = d, bal, False
            log.append({"date": str(d), "balance": round(bal, 2)})

        # ---- target: measured flat, at a day close, like the firms do
        profit = bal - rules.starting_balance
        if profit >= rules.profit_target and positions[i] == 0:
            days = trading_days + (1 if traded_today else 0)
            pnls = day_pnls + ([day_pnl] if traded_today else [])
            if days < rules.min_trading_days:
                continue                  # keep trading; the target alone is not a pass
            if rules.min_winning_days:
                wins = sum(1 for p in pnls if p >= rules.winning_day_min)
                if wins < rules.min_winning_days:
                    continue
            if rules.consistency_pct and pnls:
                best = max(pnls)
                if profit > 0 and best / profit > rules.consistency_pct:
                    return _fail(FAIL_CONSISTENCY, i, bars, start, bal, pnls,
                                 days, rules,
                                 extra={"best_day": round(best, 2),
                                        "share": round(best / profit, 3)})
            return {"result": "PASS", "reason": "", "profit": round(profit, 2),
                    "balance": round(bal, 2), "days": days,
                    "calendar_days": (bars[i]["time"] - bars[start]["time"]) // 86_400_000,
                    "bars": i - start, "overnight_holds": overnight_holds,
                    "start_date": str(_day_of(bars[start]["time"])),
                    "end_date": str(_day_of(bars[i]["time"]))}

    return _fail(FAIL_TIME, end - 1, bars, start, bal, day_pnls, trading_days, rules)


def _threshold_fn(rules):
    start = rules.starting_balance
    dd = rules.max_drawdown
    if rules.trail_lock == "start_plus_100":
        return lambda peak: min(peak - dd, start + 100.0)
    if rules.trail_lock == "start":
        return lambda peak: min(peak - dd, start)
    return lambda peak: peak - dd


def _fail(reason, i, bars, start, bal, day_pnls, days, rules, extra=None):
    out = {"result": "FAIL", "reason": reason,
           "profit": round(bal - rules.starting_balance, 2),
           "balance": round(bal, 2), "days": days,
           "bars": i - start,
           "calendar_days": (bars[i]["time"] - bars[start]["time"]) // 86_400_000,
           "start_date": str(_day_of(bars[start]["time"])),
           "end_date": str(_day_of(bars[i]["time"]))}
    if extra:
        out.update(extra)
    return out


def evaluate_cohorts(bars, positions, rules, contract, qty=1, *, stride=21,
                     max_bars=None, worst_case=True, enforce_overnight=True,
                     notional=None):
    """Run the evaluation from many different start dates.

    One evaluation is an anecdote: pass or fail depends heavily on which week you
    happened to start. Starting every `stride` bars and reporting the DISTRIBUTION
    is the honest version — a strategy that passes 30% of the time is a strategy
    that fails 70% of the time, and both numbers are the same strategy.
    """
    starts = list(range(0, max(1, len(bars) - 60), stride))
    results = []
    for s in starts:
        e = min(len(bars), s + max_bars) if max_bars else len(bars)
        results.append(evaluate(bars, positions, rules, contract, qty, start=s,
                                end=e, worst_case=worst_case,
                                enforce_overnight=enforce_overnight,
                                notional=notional))
    passes = [r for r in results if r["result"] == "PASS"]
    fails = [r for r in results if r["result"] == "FAIL"]
    reasons = {}
    for r in fails:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    return {
        "cohorts": len(results),
        "pass_rate": round(len(passes) / len(results), 3) if results else 0.0,
        "median_days_to_pass": (round(statistics.median([p["days"] for p in passes]), 1)
                                if passes else None),
        "median_calendar_days": (round(statistics.median(
            [p["calendar_days"] for p in passes]), 1) if passes else None),
        "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "results": results,
        "rules": rules.label,
    }


def sweep_size(bars, positions_by_name, rules, contract, qtys, *, stride=21,
               max_bars=None, enforce_overnight=True, notional=None):
    """Pass rate for every strategy at every position size.

    This is the map that matters at a prop firm, and it is the one nobody runs.
    The trailing drawdown is a FIXED number of dollars, so doubling size doubles
    every swing against a line that never moves — pass probability collapses long
    before expectancy does. The best size is usually the smallest one that can
    still reach the target in the time available, and it is almost never the
    maximum the firm allows.
    """
    cols = [{"label": f"{q}", "qty": q} for q in qtys]
    rows = []
    for name, pos in positions_by_name.items():
        cells = []
        for q in qtys:
            c = evaluate_cohorts(bars, pos, rules, contract, qty=q, stride=stride,
                                 max_bars=max_bars,
                                 enforce_overnight=enforce_overnight,
                                 notional=notional)
            cells.append({"pass_pct": round(c["pass_rate"] * 100, 1),
                          "cohorts": c["cohorts"],
                          "median_days": c["median_days_to_pass"],
                          "top_reason": next(iter(c["failure_reasons"]), "-"),
                          "failure_reasons": c["failure_reasons"]})
        rows.append({"strategy": name, "cells": cells})
    rows.sort(key=lambda r: max(c["pass_pct"] for c in r["cells"]), reverse=True)
    return {"cols": cols, "rows": rows, "metric": "pass_pct",
            "rules": rules.label, "contract": contract.symbol}


def print_size_sweep(sweep):
    print(f"\n  pass rate by position size ({sweep['contract']}, "
          f"{sweep['rules']}) — the trailing drawdown is a fixed $ amount, so\n"
          f"  size is the main lever you actually control:")
    print(f"  {'strategy':<20}" + "".join(f"{c['label'] + ' ct':>9}" for c in sweep["cols"]))
    for r in sweep["rows"]:
        print(f"  {r['strategy']:<20}" +
              "".join(f"{c['pass_pct']:>8.0f}%" for c in r["cells"]))


# ------------------------------------------------------------- live guardrail

class PropGuard:
    """Enforces the same rules on a LIVE/paper account, not just in backtest.

    The backtest tells you whether a strategy could pass. This stops the account
    from dying while it tries: it refuses orders that would exceed the contract
    cap, and halts trading the moment equity approaches the trailing threshold or
    the daily loss limit — with a configurable buffer, because being flat one
    tick before the line is the same as being flat one tick after it, except the
    account still exists.
    """

    def __init__(self, rules, contract=None, buffer_usd=100.0):
        self.rules = rules
        self.contract = contract
        self.buffer = buffer_usd
        self.balance = rules.starting_balance
        self.peak = rules.starting_balance
        self.day_start = rules.starting_balance
        self.threshold_of = _threshold_fn(rules)
        self.halted = False
        self.halt_reason = ""
        self.day = None

    @property
    def threshold(self):
        return self.threshold_of(self.peak)

    def mark(self, equity, now_ms=None):
        """Call on every price update with CURRENT equity (realized + unrealized)."""
        if now_ms is not None:
            d = _day_of(now_ms)
            if self.day is None:
                self.day = d
            elif d != self.day:
                if self.rules.trail_mode == "eod":
                    self.peak = max(self.peak, equity)
                self.day, self.day_start = d, equity
        if self.rules.trail_mode == "intraday":
            self.peak = max(self.peak, equity)
        self.balance = equity

        thr = self.threshold
        if equity <= thr + self.buffer:
            self._halt(f"equity ${equity:,.0f} within ${self.buffer:,.0f} of the "
                       f"trailing threshold ${thr:,.0f}")
        if self.rules.daily_loss_limit is not None:
            loss = equity - self.day_start
            if loss <= -(self.rules.daily_loss_limit - self.buffer):
                self._halt(f"daily P&L ${loss:,.0f} within ${self.buffer:,.0f} of "
                           f"the ${self.rules.daily_loss_limit:,.0f} daily loss limit")
        return not self.halted

    def check_order(self, contracts_after):
        if self.halted:
            return False, f"PROP HALT: {self.halt_reason}"
        if abs(contracts_after) > self.rules.max_contracts:
            return False, (f"{abs(contracts_after)} contracts exceeds the "
                           f"{self.rules.label} cap of {self.rules.max_contracts}")
        return True, ""

    def _halt(self, reason):
        if not self.halted:
            self.halted, self.halt_reason = True, reason

    def snapshot(self):
        return {"firm": self.rules.label, "balance": round(self.balance, 2),
                "peak": round(self.peak, 2), "threshold": round(self.threshold, 2),
                "room": round(self.balance - self.threshold, 2),
                "day_pnl": round(self.balance - self.day_start, 2),
                "halted": self.halted, "halt_reason": self.halt_reason}


def suggest_notional(bars, contract):
    """A plausible per-contract notional when the series is a proxy.

    Returns None when the series already looks like the contract itself (its
    price times point value lands in a sane notional range), so the honest
    points-based math is used and nothing is silently rescaled.
    """
    px = statistics.median([b["close"] for b in bars])
    native = px * contract.point_value
    if 5_000 <= native <= 2_000_000:
        return None
    return {"MNQ": 40_000, "NQ": 400_000, "MES": 30_000, "ES": 300_000,
            "M2K": 11_000, "RTY": 110_000, "MYM": 21_000, "YM": 210_000,
            "MGC": 27_000, "GC": 270_000, "MCL": 7_000, "CL": 70_000,
            }.get(contract.symbol, 40_000)


def print_report(rows, rules, contract, qty, notional=None):
    """Console table: would each strategy have passed, and how often?"""
    sizing = (f"${notional:,.0f} notional/contract (proxy series)" if notional
              else f"${contract.point_value:g}/pt")
    print(f"\n=== PROP EVALUATION: {rules.label} "
          f"({contract.symbol} x{qty}, {sizing}, "
          f"${contract.commission_rt:g} RT) ===")
    print(f"  target ${rules.profit_target:,.0f} · trailing drawdown "
          f"${rules.max_drawdown:,.0f} ({rules.trail_mode}, lock={rules.trail_lock})"
          + (f" · daily loss limit ${rules.daily_loss_limit:,.0f}"
             if rules.daily_loss_limit else " · no daily loss limit"))
    print(f"  {'strategy':<20}{'pass%':>7}{'cohorts':>9}{'medDays':>9}  top failure reason")
    for r in rows:
        c = r["cohort"]
        top = next(iter(c["failure_reasons"]), "-")
        print(f"  {r['strategy']:<20}{c['pass_rate']*100:>6.0f}%{c['cohorts']:>9}"
              f"{str(c['median_days_to_pass'] or '-'):>9}  {top}")
    print(f"  Rules snapshot: {rules.as_of}. {rules.notes}")
