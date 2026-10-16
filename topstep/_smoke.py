"""Offline self-test. No network, no API key, no credentials.

    python3 -m topstep._smoke

Covers the parts where a silent error costs real money: contract arithmetic,
the trailing-drawdown state machine, every rejection path in the governor,
the consistency rule, and the live locks.
"""

from __future__ import annotations

import sys

from . import fixtures as fx
from .backtest import run, monte_carlo
from .instruments import get, INSTRUMENTS
from .live import RiskGovernor, PaperBroker, build_broker
from .rules import TIERS, AccountSpec, AccountState, Breach
from .strategies import STRATEGIES, Signal

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'ok  ' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def main():
    print("TopStep toolkit smoke test (offline)\n")

    # 1. instrument arithmetic ----------------------------------------------
    print("contract specs")
    mes, es, mnq = get("MES"), get("ES"), get("MNQ")
    check("MES point value = $5", mes.point_value == 5.0)
    check("ES tick is 10x MES tick", es.tick_value == 10 * mes.tick_value)
    check("4 MES, +10 ticks = $50 gross",
          mes.pnl(5000, 5000 + 10 * mes.tick_size, 4, True) == 50.0,
          f"${mes.pnl(5000, 5000 + 10 * mes.tick_size, 4, True):.2f}")
    check("short P&L sign is inverted",
          mes.pnl(5000, 4990, 1, False) > 0 > mes.pnl(5000, 4990, 1, True))
    check("contract id parses to product", get("CON.F.US.MNQ.Z25") is mnq)
    check("unknown symbol raises", _raises(lambda: get("NOPE"), KeyError))

    # 2. trailing drawdown --------------------------------------------------
    print("\ntrailing drawdown")
    st = AccountState(TIERS["50K"]); st.start_day("d1")
    check("day 1 floor is the DAILY limit, not the trailing one",
          st.effective_floor == 49_000.0 and st.room == 1000.0,
          f"floor ${st.effective_floor:,.0f}")
    st.on_trade_closed(2500.0); st.start_day("d2")
    check("floor ratchets up after a winning day", st.mll_floor == 50_000.0,
          f"peak ${st.peak_balance:,.0f} -> floor ${st.mll_floor:,.0f}")
    check("floor locks at breakeven, does not trail past it",
          st.mll_floor == st.spec.starting_balance)
    breached = _raises(lambda: st.on_trade_closed(-2600.0), Breach)
    check("net-positive account can still breach", breached and st.profit < 0,
          f"profit ${st.profit:,.0f} yet dead")
    check("breach is terminal (further trades raise)",
          _raises(lambda: st.on_trade_closed(10.0), Breach))

    # intraday vs eod trailing
    spec_i = AccountSpec("t", 50_000, 3_000, 2_000, 1_000, 5, trail_mode="intraday")
    si = AccountState(spec_i); si.start_day("d1")
    si.mark(1500.0)
    check("intraday mode trails unrealized equity", si.peak_balance == 51_500.0,
          f"peak ${si.peak_balance:,.0f}")
    se = AccountState(TIERS["50K"]); se.start_day("d1"); se.mark(1500.0)
    check("eod mode ignores unrealized for the peak", se.peak_balance == 50_000.0)

    # 3. daily loss limit ---------------------------------------------------
    print("\ndaily loss limit")
    st2 = AccountState(TIERS["50K"]); st2.start_day("d1")
    check("DLL breach fires at the day floor",
          _raises(lambda: st2.on_trade_closed(-1100.0), Breach))
    st3 = AccountState(TIERS["50K"]); st3.start_day("d1"); st3.on_trade_closed(-900.0)
    st3.start_day("d2")
    check("new day resets the daily floor", st3.room > 0 and st3.dll_floor == 48_100.0,
          f"room ${st3.room:,.0f}")

    # 4. consistency rule ---------------------------------------------------
    print("\nconsistency rule")
    c = AccountState(TIERS["50K"])
    for d, p in (("d1", 2500.0), ("d2", 500.0)):
        c.start_day(d); c.on_trade_closed(p)
    check("one outsized day violates the cap", c.consistency_violation() is not None,
          c.consistency_violation())
    check("needed profit to clear it is computed",
          abs(c.min_profit_for_consistency() - 5000.0) < 1e-6,
          f"${c.min_profit_for_consistency():,.0f} total needed")
    c2 = AccountState(TIERS["50K"])
    for d, p in (("d1", 1000.0), ("d2", 1000.0), ("d3", 1000.0)):
        c2.start_day(d); c2.on_trade_closed(p)
    check("evenly spread profit passes", c2.consistency_violation() is None)
    check("hitting target with even days => passed", c2.passed)

    # 5. sizing -------------------------------------------------------------
    print("\nposition sizing")
    s5 = AccountState(TIERS["50K"]); s5.start_day("d1")
    q = s5.max_contracts_for_risk(mes, 12, 0.25)
    worst = q * (12 * mes.tick_value + mes.commission_rt)
    check("size keeps worst case inside the risk budget",
          worst <= s5.room * 0.25 + 1e-9, f"{q}x -> ${worst:.2f} of ${s5.room * .25:.0f}")
    check("wider stop => smaller size",
          s5.max_contracts_for_risk(mes, 48, .25) < s5.max_contracts_for_risk(mes, 12, .25))
    check("micro cap respected",
          s5.max_contracts_for_risk(mes, 1, .25) <= TIERS["50K"].micro_max_contracts)
    check("zero stop is unsizeable", s5.max_contracts_for_risk(mes, 0, .25) == 0)

    # 6. governor -----------------------------------------------------------
    print("\nrisk governor")
    g = RiskGovernor(AccountState(TIERS["50K"]), mes); g.start_day("d1")
    ok = g.approve(Signal("long", 5000, 5000 - 12 * .25, 5010), 600)
    check("valid signal approved with a size", ok.approved and ok.qty > 0, ok.reason)
    check("stopless signal rejected",
          not g.approve(Signal("long", 5000, 5000, 5010), 600).approved)
    check("too-wide stop rejected",
          not g.approve(Signal("long", 5000, 5000 - 200 * .25, 5030), 600).approved)
    check("past flatten time rejected",
          not g.approve(Signal("long", 5000, 4997, 5006), 960).approved)
    for _ in range(g.max_trades_per_day):
        g.on_fill()
    check("daily trade cap rejected",
          not g.approve(Signal("long", 5000, 4997, 5006), 600).approved)
    g2 = RiskGovernor(AccountState(TIERS["50K"]), mes); g2.start_day("d1")
    g2.state.on_trade_closed(-880.0)
    check("refuses to trade on fumes",
          not g2.approve(Signal("long", 5000, 4997, 5006), 600).approved,
          f"room ${g2.state.room:,.0f}")
    g3 = RiskGovernor(AccountState(TIERS["50K"]), mes); g3.start_day("d1")
    g3.state.on_trade_closed(3100.0)
    check("stops trading once the target is hit",
          not g3.approve(Signal("long", 5000, 4997, 5006), 600).approved)

    # 7. paper broker costs -------------------------------------------------
    print("\npaper fills")
    b = PaperBroker(mes, slippage_ticks=1)
    b.enter("long", 4, 5000.0)
    r = b.exit(5000.0, "flat")
    check("a flat round trip loses money", r["net"] < 0,
          f"${r['net']:.2f} on 4 MES (slippage + ${r['commission']:.2f} commission)")

    # 8. backtest -----------------------------------------------------------
    print("\nbacktest")
    bars = fx.winning_bars(6, seed=11)
    res = run(bars, STRATEGIES["trend_pullback"], mes, TIERS["50K"])
    check("backtest completes and books trades", len(res.trades) > 0,
          f"{len(res.trades)} trades over {res.days} sessions")
    check("commissions charged on every trade",
          all(t.commission > 0 for t in res.trades))
    check("no position survives the session",
          all(t.exit_reason != "open" for t in res.trades))
    check("summary reports a result",
          res.summary()["net_pnl"] is not None)

    # 9. monte carlo --------------------------------------------------------
    print("\nmonte carlo")
    good = monte_carlo(TIERS["50K"], win_rate=.55, avg_win=200, avg_loss=100,
                       sims=800)
    bad = monte_carlo(TIERS["50K"], win_rate=.40, avg_win=100, avg_loss=100,
                      sims=800)
    check("a real edge passes more often than a losing one",
          good["pass_rate_pct"] > bad["pass_rate_pct"],
          f"{good['pass_rate_pct']}% vs {bad['pass_rate_pct']}%")
    check("negative expectancy is reported negative",
          bad["expectancy_per_trade"] < 0)
    big = monte_carlo(TIERS["50K"], win_rate=.5, avg_win=1000, avg_loss=500,
                      sims=800)
    check("oversizing lowers the pass rate despite higher expectancy",
          big["expectancy_per_trade"] > good["expectancy_per_trade"]
          and big["pass_rate_pct"] < good["pass_rate_pct"],
          f"exp ${big['expectancy_per_trade']:.0f} but only "
          f"{big['pass_rate_pct']}% pass")

    # 10. live locks --------------------------------------------------------
    print("\nlive locks")
    import os
    os.environ.pop("TOPSTEP_ALLOW_LIVE", None)
    check("lock 1: TOPSTEP_ALLOW_LIVE required",
          _raises(lambda: build_broker("live", mes), PermissionError))
    os.environ["TOPSTEP_ALLOW_LIVE"] = "1"
    lb = build_broker("live", mes)
    check("lock 2: live routing not implemented",
          _raises(lambda: lb.enter("long", 1, 5000), NotImplementedError))
    os.environ.pop("TOPSTEP_ALLOW_LIVE", None)
    check("paper mode is the default",
          build_broker("paper", mes).name == "paper")

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("all checks passed")
    return 0


def _raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
