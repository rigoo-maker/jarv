"""TopStep toolkit CLI.

    python3 -m topstep.app doctor                    config + endpoint reachability
    python3 -m topstep.app rules --tier 50K          show the limits you trade under
    python3 -m topstep.app size --tier 50K --stop 12 position size for a stop
    python3 -m topstep.app montecarlo --win 0.5 --rr 2 --risk 100
    python3 -m topstep.app sweep --win 0.5 --rr 2    find the viable risk band
    python3 -m topstep.app backtest --strategy orb --days 20
"""

from __future__ import annotations

import argparse
import sys

from .instruments import get as get_instrument, INSTRUMENTS
from .rules import TIERS, AccountState
from .backtest import monte_carlo, run
from .strategies import STRATEGIES


def cmd_doctor(a):
    import os
    from .api import TopstepClient, BlockedError, ApiError, BASE_URL
    print("TopStep toolkit doctor\n")
    print(f"  api base          {BASE_URL}")
    print(f"  username set      {bool(os.environ.get('TOPSTEP_USERNAME'))}")
    print(f"  api key set       {bool(os.environ.get('TOPSTEP_API_KEY'))}")
    print(f"  mode              {os.environ.get('TOPSTEP_MODE', 'paper')}")
    print(f"  live unlocked     {os.environ.get('TOPSTEP_ALLOW_LIVE', '0')} "
          f"(live routing is NOT implemented regardless)")
    print(f"  instruments       {', '.join(sorted(INSTRUMENTS))}")
    print("\n  gateway reachability:")
    c = TopstepClient()
    try:
        c.login()
        print("    [ok  ] auth")
        print(f"    [ok  ] accounts: {len(c.accounts())}")
    except BlockedError as e:
        print(f"    [FAIL] {str(e)[:100]}")
        print("\n  A blocked host is network policy, not a bug. Run where "
              "api.topstepx.com is reachable.")
    except ApiError as e:
        print(f"    [FAIL] {e}")
    return 0


def cmd_rules(a):
    spec = TIERS[a.tier]
    st = AccountState(spec)
    st.start_day("today")
    print(f"\n{spec.name} Trading Combine\n" + "-" * 52)
    print(f"  starting balance     ${spec.starting_balance:>10,.0f}")
    print(f"  profit target        ${spec.profit_target:>10,.0f}")
    print(f"  trailing max loss    ${spec.max_loss_limit:>10,.0f}  ({spec.trail_mode})")
    print(f"  daily loss limit     ${spec.daily_loss_limit:>10,.0f}")
    print(f"  max contracts        {spec.max_contracts:>11} "
          f"({spec.micro_max_contracts} micros)")
    print(f"  consistency cap      {spec.consistency_pct:>10.0f}% of profit on one day")
    print(f"  min trading days     {spec.min_trading_days:>11}")
    print(f"\n  day 1 floor          ${st.effective_floor:>10,.2f}  "
          f"(the DAILY limit binds first, not the trailing one)")
    print(f"  room to trade        ${st.room:>10,.2f}")
    print(f"\n  target:drawdown ratio {spec.profit_target / spec.max_loss_limit:.2f}:1 "
          f"— you must make {spec.profit_target / spec.max_loss_limit:.1f}x your "
          f"maximum tolerable loss.")
    return 0


def cmd_size(a):
    spec = TIERS[a.tier]
    inst = get_instrument(a.symbol)
    st = AccountState(spec)
    st.start_day("today")
    if a.profit:
        st.balance += a.profit
        st.peak_balance = max(st.peak_balance, st.balance)
        st.day_start_balance = st.balance
    print(f"\n{inst.symbol} @ {a.tier}  (profit so far ${st.profit:,.0f})")
    print(f"  room ${st.room:,.2f}   floor ${st.effective_floor:,.2f}\n")
    print(f"{'stop(ticks)':>12}{'$/contract':>12}{'qty @25%':>10}{'qty @50%':>10}"
          f"{'risk @25%':>11}")
    print("-" * 55)
    for ticks in (a.stop and [a.stop] or [4, 8, 12, 16, 24, 32, 48]):
        per = ticks * inst.tick_value
        q25 = st.max_contracts_for_risk(inst, ticks, 0.25)
        q50 = st.max_contracts_for_risk(inst, ticks, 0.50)
        print(f"{ticks:>12}{per:>12.2f}{q25:>10}{q50:>10}"
              f"{q25 * (per + inst.commission_rt):>11.2f}")
    return 0


def cmd_montecarlo(a):
    spec = TIERS[a.tier]
    r = monte_carlo(spec, win_rate=a.win, avg_win=a.risk * a.rr,
                    avg_loss=a.risk, trades_per_day=a.trades,
                    max_days=a.days, sims=a.sims,
                    commission=get_instrument(a.symbol).commission_rt)
    print(f"\n{a.tier}: {a.win:.0%} win rate, {a.rr}:1 R:R, ${a.risk:.0f} risk, "
          f"{a.trades} trades/day, {a.days}-day limit, {a.sims:,} sims\n")
    for k in ("expectancy_per_trade", "pass_rate_pct", "breach_mll_pct",
              "breach_dll_pct", "consistency_blocked_pct", "timeout_pct",
              "median_days_to_pass", "mean_final_profit"):
        print(f"  {k:<26} {r[k]}")
    if r["pass_rate_pct"] < 50:
        print("\n  Under 50%. Either the edge is not there or the size is wrong "
              "— run `sweep` to see which.")
    return 0


def cmd_sweep(a):
    spec = TIERS[a.tier]
    comm = get_instrument(a.symbol).commission_rt
    print(f"\n{a.tier}: {a.win:.0%} win, {a.rr}:1 R:R — sweeping risk per trade\n")
    print(f"{'risk':>7}{'exp/trade':>11}{'PASS%':>8}{'MLL%':>7}{'DLL%':>7}"
          f"{'timeout%':>10}  binding")
    print("-" * 66)
    best = (None, -1)
    for risk in (25, 50, 75, 100, 150, 200, 250, 300, 400, 500):
        r = monte_carlo(spec, win_rate=a.win, avg_win=risk * a.rr, avg_loss=risk,
                        trades_per_day=a.trades, max_days=a.days, sims=a.sims,
                        commission=comm)
        worst = max([("timeout", r["timeout_pct"]),
                     ("daily loss", r["breach_dll_pct"]),
                     ("trailing MLL", r["breach_mll_pct"])], key=lambda x: x[1])
        note = worst[0] if worst[1] > 5 else "—"
        if r["pass_rate_pct"] > best[1]:
            best = (risk, r["pass_rate_pct"])
        print(f"${risk:>6}{r['expectancy_per_trade']:>11.2f}{r['pass_rate_pct']:>8.1f}"
              f"{r['breach_mll_pct']:>7.1f}{r['breach_dll_pct']:>7.1f}"
              f"{r['timeout_pct']:>10.1f}  {note}")
    print(f"\n  best pass rate: ${best[0]} per trade ({best[1]:.1f}%)")
    print("  Too small and you time out; too large and the daily limit ends you.")
    return 0


def cmd_backtest(a):
    from .fixtures import winning_bars, chop_bars, session_bars
    spec = TIERS[a.tier]
    inst = get_instrument(a.symbol)
    tapes = {"trend": winning_bars, "chop": chop_bars, "neutral": session_bars}
    bars = tapes[a.tape](a.days, seed=a.seed)
    names = [a.strategy] if a.strategy else list(STRATEGIES)
    print(f"\n{a.tape} fixture · {len(bars)} bars · {inst.symbol} · {a.tier}\n")
    print(f"{'strategy':<16}{'trades':>7}{'win%':>7}{'net':>9}{'comm':>8}"
          f"{'maxDD':>8}  outcome")
    print("-" * 72)
    for name in names:
        r = run(bars, STRATEGIES[name], inst, spec, risk_fraction=a.risk_fraction)
        s = r.summary()
        out = ("PASSED" if r.passed else
               ("BREACH " + r.breached.split(":")[0]) if r.breached else "no pass")
        print(f"{name:<16}{s['trades']:>7}{(s['win_rate_pct'] or 0):>7.1f}"
              f"{s['net_pnl']:>9.0f}{s['commissions']:>8.0f}"
              f"{s['max_drawdown']:>8.0f}  {out}")
    print("\n  Fixtures are synthetic random walks — they contain no edge by "
          "construction.\n  Losses here are the correct result; they prove the "
          "cost model bites.\n  Replace with real bars via `TopstepClient.bars()` "
          "to learn anything about a strategy.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="topstep", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, tier=True, sym=True):
        if tier:
            p.add_argument("--tier", default="50K", choices=list(TIERS))
        if sym:
            p.add_argument("--symbol", default="MES")
        return p

    common(sub.add_parser("doctor"), tier=False, sym=False)
    common(sub.add_parser("rules"), sym=False)
    p = common(sub.add_parser("size"))
    p.add_argument("--stop", type=int, help="stop distance in ticks")
    p.add_argument("--profit", type=float, default=0.0, help="profit so far")
    for name in ("montecarlo", "sweep"):
        p = common(sub.add_parser(name))
        p.add_argument("--win", type=float, default=0.5)
        p.add_argument("--rr", type=float, default=2.0)
        p.add_argument("--trades", type=int, default=3)
        p.add_argument("--days", type=int, default=60)
        p.add_argument("--sims", type=int, default=4000)
        if name == "montecarlo":
            p.add_argument("--risk", type=float, default=100.0)
    p = common(sub.add_parser("backtest"))
    p.add_argument("--strategy", choices=list(STRATEGIES))
    p.add_argument("--tape", default="trend", choices=["trend", "chop", "neutral"])
    p.add_argument("--days", type=int, default=20)
    p.add_argument("--seed", type=int, default=11)
    p.add_argument("--risk-fraction", type=float, default=0.25)

    a = ap.parse_args(argv)
    return {"doctor": cmd_doctor, "rules": cmd_rules, "size": cmd_size,
            "montecarlo": cmd_montecarlo, "sweep": cmd_sweep,
            "backtest": cmd_backtest}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
