"""SOLAI command line.

    python3 -m solai.app doctor                 # config + endpoint reachability
    python3 -m solai.app scan                   # one scan, ranked table
    python3 -m solai.app watch --interval 300   # loop; warms the TA history
    python3 -m solai.app paper --interval 300   # loop + paper trades
    python3 -m solai.app record                 # the track record
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import engine, venue
from .config import load_config
from .paper import Portfolio
from .pricelog import PriceLog
from .sources import dexscreener, jupiter, rpc


def _fmt_usd(v):
    if v is None:
        return "     -"
    if v >= 1_000_000:
        return f"${v/1e6:,.1f}M"
    if v >= 1_000:
        return f"${v/1e3:,.0f}k"
    return f"${v:,.0f}"


def cmd_doctor(cfg, args):
    print("SOLAI doctor\n")
    print(f"  mode              {cfg.mode}")
    print(f"  live unlocked     {cfg.live_unlocked}  "
          f"(live execution is NOT implemented regardless)")
    print(f"  equity            ${cfg.risk.equity_usd:,.2f}")
    print(f"  per-trade cap     ${cfg.risk.max_position_usd:,.2f} "
          f"({cfg.risk.max_position_pct:.0f}%)")
    print(f"  assumed costs     {cfg.risk.round_trip_cost_pct:.2f}% round trip "
          f"({cfg.risk.fee_bps:.0f}bps fee + {cfg.risk.slippage_bps:.0f}bps slip)")
    print(f"  state dir         {cfg.state_dir}")

    from . import analyst as A
    print(f"\n  analyst           {'enabled' if cfg.analyst_enabled else 'disabled'}"
          f" | anthropic SDK {'installed' if A.available() else 'NOT installed'}"
          f" | model {cfg.analyst_model}")

    print("\n  endpoint reachability:")
    checks = [
        ("dexscreener", lambda: dexscreener.token_profiles(cfg.dexscreener_url)),
        ("jupiter", lambda: jupiter.prices(cfg.jupiter_url,
                                           ["So11111111111111111111111111111111111111112"])),
        ("solana rpc", lambda: rpc._rpc(cfg.rpc_url, "getHealth", [])),
    ]
    for name, fn in checks:
        try:
            fn()
            print(f"    [ok  ] {name}")
        except Exception as e:
            print(f"    [FAIL] {name}: {type(e).__name__}: {str(e)[:90]}")
    print("\n  A blocked host means network policy, not a bug. Run where "
          "these are reachable.")
    return 0


def _print_table(res, cfg):
    print(f"\nscanned {res['scanned']}  |  passed safety {res.get('passed_safety', 0)}"
          f"  |  analyst calls {res.get('analyst_calls', 0)}")
    header = (f"{'SYMBOL':<10}{'SCORE':>7}{'CONF':>6}{'LIQ':>9}{'VOL24':>9}"
              f"{'RT%':>7}  {'ACTION':<7}REASON")
    print(header)
    print("-" * len(header))

    for c in res["candidates"]:
        sc = c["score"] or {}
        micro = c["bundle"].micro or {}
        ex = c["bundle"].execution or {}
        rt = ex.get("round_trip_cost_pct")
        decision = c["decision"] or {}

        score_s = f"{sc['score']:.1f}" if sc.get("score") is not None else "-"
        conf_s = f"{sc['confidence']:.2f}" if sc.get("confidence") is not None else "-"
        rt_s = f"{rt:.2f}" if rt is not None else "-"

        print(f"{(c['symbol'] or c['mint'][:8]):<10}"
              f"{score_s:>7}{conf_s:>6}"
              f"{_fmt_usd(micro.get('liquidity_usd')):>9}"
              f"{_fmt_usd(micro.get('volume_h24')):>9}"
              f"{rt_s:>7}  "
              f"{decision.get('action', '-'):<7}"
              f"{decision.get('reason', '')[:52]}")

        analyst = c.get("analyst") or {}
        if analyst.get("thesis") and not analyst.get("_error"):
            print(f"{'':<12}analyst {analyst.get('verdict')}: "
                  f"{analyst['thesis'][:80]}")
            for flag in (analyst.get("red_flags") or [])[:3]:
                print(f"{'':<14}- {flag[:86]}")
        for caveat in (c["safety"] or {}).get("caveats", []):
            print(f"{'':<12}! {caveat[:88]}")


def cmd_scan(cfg, args):
    res = engine.scan(cfg, cfg.mints or None)
    if args.json:
        print(json.dumps({**res, "candidates": [
            {k: v for k, v in c.items() if k != "bundle"}
            for c in res["candidates"]]}, indent=2, default=str))
        return 0
    _print_table(res, cfg)
    return 0


def cmd_watch(cfg, args):
    log = PriceLog(cfg.state_dir)
    n = 0
    print(f"watching every {args.interval}s — this is also how the TA engine "
          f"warms up (needs ~60 bars). ctrl-c to stop.\n")
    while True:
        n += 1
        try:
            res = engine.scan(cfg, cfg.mints or None, price_log=log)
            print(f"[{time.strftime('%H:%M:%S')}] cycle {n}")
            _print_table(res, cfg)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] cycle failed: "
                  f"{type(e).__name__}: {e}")
        time.sleep(args.interval)


def cmd_paper(cfg, args):
    if cfg.mode == "live":
        print("refusing: `paper` command with SOLAI_MODE=live. "
              "Live execution is not implemented.")
        return 2
    cfg.mode = "paper"
    pf = Portfolio(cfg, venue.build(cfg))
    log = PriceLog(cfg.state_dir)
    print(f"paper trading. equity ${pf.equity({}):,.2f}, "
          f"{len(pf.positions)} open. ctrl-c to stop.\n")
    while True:
        try:
            res = engine.run_paper(cfg, pf, cfg.mints or None, price_log=log)
            print(f"[{time.strftime('%H:%M:%S')}] equity ${res['equity']:,.2f}")
            for e in res["exits"]:
                print(f"    EXIT  {e.get('symbol')} {e['reason']} "
                      f"{e['pnl_pct']:+.1f}% (${e['pnl']:+.2f})")
            for e in res["entries"]:
                if e.get("opened"):
                    print(f"    ENTER {e['symbol']} @ "
                          f"${e['fill']['price']:.8f} (${e['fill']['usd']:.2f})")
                else:
                    print(f"    skip  {e['symbol']}: {e['reason']}")
            if pf.halted:
                print(f"    HALTED: {pf.halted} — no new entries")
            if args.once:
                _print_record(pf)
                return 0
        except KeyboardInterrupt:
            _print_record(pf)
            return 0
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] cycle failed: "
                  f"{type(e).__name__}: {e}")
        time.sleep(args.interval)


def _print_record(pf):
    s = pf.stats({})
    print("\ntrack record")
    for k in ("equity", "total_return_pct", "trades", "win_rate_pct",
              "avg_win_pct", "avg_loss_pct", "profit_factor",
              "expectancy_pct", "sharpe", "max_drawdown_pct", "halted"):
        print(f"  {k:<20} {s.get(k)}")
    if (s.get("trades") or 0) < 30:
        print("\n  Fewer than 30 trades: this is not yet evidence of anything. "
              "Win rate on a handful of trades is noise.")


def cmd_record(cfg, args):
    pf = Portfolio(cfg, venue.build(cfg))
    _print_record(pf)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="solai", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["doctor", "scan", "watch", "paper", "record"])
    ap.add_argument("--mints", nargs="*", help="explicit mint watchlist")
    ap.add_argument("--interval", type=int, default=300, help="loop seconds")
    ap.add_argument("--equity", type=float, help="override account size in USD")
    ap.add_argument("--no-analyst", action="store_true",
                    help="skip the Claude layer (scorer only)")
    ap.add_argument("--once", action="store_true", help="paper: single cycle")
    ap.add_argument("--json", action="store_true", help="scan: raw JSON output")
    ap.add_argument("--state-dir", help="override state directory")
    args = ap.parse_args(argv)

    cfg = load_config(state_dir=args.state_dir)
    if args.mints:
        cfg.mints = args.mints
    if args.no_analyst:
        cfg.analyst_enabled = False
    if args.equity:
        cfg.risk.equity_usd = args.equity

    return {"doctor": cmd_doctor, "scan": cmd_scan, "watch": cmd_watch,
            "paper": cmd_paper, "record": cmd_record}[args.command](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
