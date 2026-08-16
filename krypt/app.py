"""KRYPT CLI entry point.

Subcommands:
  analyze   one-shot read-only snapshot -> writes krypt_dashboard.html
  serve     live dashboard at http://localhost:PORT (auto-refresh) + trading loop
  backtest  run the trend strategy over historical candles, report stats
  compare   rank all 10 strategies over one sample
  heatmap   edge-decay / regime / cost / correlation maps -> krypt_heatmaps.html
  ninja     export the surviving strategies as NinjaTrader 8 NinjaScript (.cs)

Mode (analyze|paper|live) and all risk limits come from env / config.py.
LIVE requires the two-lock guard (see config.assert_live_allowed).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config
from .binance_client import BinanceClient, BinanceError
from .risk import RiskEngine
from .trader import Trader
from .alerts import default_rules
from .strategies import make_strategy, REGISTRY as LIVE_STRATEGIES
from .engine import Engine
from . import dashboard, live_dashboard, indicators, data as datamod, backtest as bt
from . import strats as stratlib
from . import analytics as ana
from . import heatmap as heatmapmod
from . import ninjascript as nj
from . import csvdata
from . import strats_equity as eqlib
from . import propfirm as prop
from . import hybrids as hyb
from . import validate as val
from .scoring import score_snapshot
from datetime import date, timedelta


def build(cfg):
    client = BinanceClient(cfg)              # public data (klines/order book)
    exec_client = client
    if cfg.venue == "coinbase":
        from .coinbase_client import CoinbaseClient
        exec_client = CoinbaseClient(cfg)    # live orders routed here
    risk = RiskEngine(limits=cfg.risk)
    trader = Trader(cfg, client, risk, exec_client=exec_client)
    strat = make_strategy(cfg.strategy, cfg, client)
    alerts = default_rules(cfg.symbols[0])
    return client, risk, trader, strat, alerts, Engine(cfg, client, strat, trader, risk, alerts)


def cmd_analyze(cfg):
    client, risk, trader, strat, alerts, eng = build(cfg)
    sym = cfg.symbols[0]
    state = eng.tick(sym)
    with open("krypt_dashboard.html", "w") as f:
        f.write(dashboard.render(state, cfg.refresh_secs))
    sc = state["scoring"]
    print(f"[{sym}] ${state['price']}  score={sc['score']} {sc['label']}  "
          f"RSI={state['latest']['rsi'] and round(state['latest']['rsi'],1)}  "
          f"ADX={state['latest']['adx'] and round(state['latest']['adx'],1)}")
    for a in state["alerts"]:
        print("  🔔", a["msg"])
    print("✓ wrote krypt_dashboard.html")


class Handler(BaseHTTPRequestHandler):
    state_ref = {"data": {}}
    refresh = 5.0

    def log_message(self, *a):  # silence
        pass

    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(self.state_ref["data"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            html = dashboard.render(self.state_ref["data"], self.refresh).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)


def cmd_serve(cfg, port):
    client, risk, trader, strat, alerts, eng = build(cfg)
    sym = cfg.symbols[0]
    Handler.refresh = cfg.refresh_secs
    print("KRYPT  " + cfg.banner())
    if cfg.is_live and not cfg.testnet:
        print("  !! LIVE MAINNET — real orders will be placed. Ctrl-C to abort. !!")

    def loop():
        while True:
            try:
                Handler.state_ref["data"] = eng.tick(sym)
                st = Handler.state_ref["data"]
                sc = st["scoring"]
                line = (f"{time.strftime('%H:%M:%S')} {sym} ${st['price']} "
                        f"score={sc['score']} {sc['label']}")
                if st["executed"]:
                    line += "  -> " + ",".join(e.get("event", "?") for e in st["executed"])
                print(line)
                if risk.halted:
                    print("  HALTED:", risk.halt_reason, "— trading stopped.")
            except BinanceError as e:
                Handler.state_ref["data"] = {"symbol": sym, "error": str(e)}
                print("  data error:", e)
            time.sleep(cfg.refresh_secs)

    threading.Thread(target=loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"  dashboard -> http://localhost:{port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


def cmd_download(cfg, days, kind):
    """Bulk-download historical data from data.binance.vision."""
    sym = cfg.symbols[0]
    end = date.today() - timedelta(days=1)        # yesterday is the last full day
    start = end - timedelta(days=days - 1)
    print(f"Downloading {kind} for {sym} {start}..{end} from data.binance.vision")
    if kind == "klines":
        candles = datamod.download_klines_range(sym, cfg.interval, start, end)
        print(f"✓ {len(candles)} candles cached in ./data (interval {cfg.interval})")
    else:
        total = 0
        for d in [start + timedelta(n) for n in range((end - start).days + 1)]:
            try:
                ticks = datamod.load_aggtrades_day(sym, d)
                total += len(ticks)
                print(f"  {d}  +{len(ticks):,} ticks (total {total:,})")
            except RuntimeError as e:
                print(f"  {d}  skipped: {e}")
        print(f"✓ {total:,} ticks (aggTrades). Resample with data.ticks_to_candles().")


def cmd_backtest(cfg, days, limit, fee_bps, slippage_bps, no_compound):
    """Realistic backtest over historical klines (bulk download if available)."""
    sym = cfg.symbols[0]
    if days:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        print(f"Loading {sym} {cfg.interval} {start}..{end} (data.binance.vision)")
        candles = datamod.download_klines_range(sym, cfg.interval, start, end)
    else:
        client = BinanceClient(cfg)
        candles = client.klines(sym, cfg.interval, min(limit, 1000))
    if len(candles) < 80:
        print("not enough candles to backtest.", file=sys.stderr)
        sys.exit(1)
    result = bt.run(candles, fee_bps=fee_bps, slippage_bps=slippage_bps,
                    compound=not no_compound)
    bt.print_report(sym, cfg.interval, result, len(candles))


def _load_candles(cfg, days, limit, args=None):
    """Candles from a local CSV, a Kaggle dataset, or Binance."""
    sym = cfg.symbols[0]
    src = getattr(args, "source", "binance") if args else "binance"
    if src in ("csv", "kaggle"):
        path = getattr(args, "file", None)
        if src == "kaggle":
            path = csvdata.download_kaggle(getattr(args, "dataset", None) or path)
        if not path:
            print("--source csv needs --file <path to csv or folder>", file=sys.stderr)
            sys.exit(1)
        candles, info = csvdata.load(path, args.symbols)
        _load_candles.last_info = info
        return candles
    if days:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        print(f"Loading {sym} {cfg.interval} {start}..{end} (data.binance.vision)")
        return datamod.download_klines_range(sym, cfg.interval, start, end)
    return BinanceClient(cfg).klines(sym, cfg.interval, min(limit, 1000))


_load_candles.last_info = None


def cmd_compare(cfg, days, limit, leverage, fee_bps, slippage_bps, no_compound):
    """Backtest all 10 strategies and rank them (the 'find the edge' step)."""
    sym = cfg.symbols[0]
    candles = _load_candles(cfg, days, limit)
    if len(candles) < 80:
        print("not enough candles to compare.", file=sys.stderr)
        sys.exit(1)
    rows = bt.compare(candles, stratlib.REGISTRY, leverage=leverage,
                      fee_bps=fee_bps, slippage_bps=slippage_bps,
                      compound=not no_compound)
    bt.print_leaderboard(sym, cfg.interval, rows, leverage, len(candles))
    if leverage >= 5:
        liq = sum(r.get("liquidations", 0) for r in rows if "liquidations" in r)
        print(f"\n  ⚠️  at {leverage}x, {liq} liquidation event(s) across strategies — "
              "this is what 'high leverage' does to a thin edge.")


def _analyze_edge(cfg, days, limit, args):
    """Shared by `heatmap` and `ninja`: load candles, run every map."""
    sym = cfg.symbols[0]
    candles = _load_candles(cfg, days, limit, args)
    info = _load_candles.last_info
    if len(candles) < 300:
        print("need at least ~300 candles for the maps (try --days 21).",
              file=sys.stderr)
        sys.exit(1)

    # Equity daily bars need equity rules: crypto minute strategies do not
    # transfer (no shorting, 200-day regime, RSI(2) not RSI(14), calendar effects).
    if args.strats == "equity":
        extras = (info or {}).get("extras")
        cache = eqlib.precompute(candles, extras)
        registry = eqlib.registry(cache)
        warmup = 300               # 200-day SMA + 52-week lookbacks must be warm
        allow_short = False        # every equity rule here is long/flat by design
        if "vix" not in (extras[0] if extras else {}):
            print("  note: no VIX column found — the two VIX rules are skipped.")
    else:
        cache, registry, warmup, allow_short = None, None, 60, not args.long_only
    interval = (info or {}).get("interval") or cfg.interval
    sym = args.symbols or (info and os.path.basename(info["path"])) or sym

    n_strats = len(registry) if registry else len(stratlib.REGISTRY)
    print(f"Analyzing {len(candles)} candles across {args.windows} windows "
          f"x {n_strats} strategies ({args.strats} library) ...")
    a = ana.Analysis(sym, interval, candles, registry, windows=args.windows,
                     fee_bps=args.fee_bps, slippage_bps=args.slippage_bps,
                     allow_short=allow_short, leverage=args.leverage,
                     oos_frac=args.oos_frac, cache=cache, extras=(info or {}).get("extras"),
                     warmup=warmup)
    return a.run_all()


def _attach_prop(report, cfg, days, limit, args):
    """Add the prop-firm pass-rate map to an existing report."""
    candles = _load_candles(cfg, days, limit, args)
    info = _load_candles.last_info
    rules = prop.load_rules(args.firm)
    contract = prop.CONTRACTS[args.contract.upper()]
    _, _, pos = _positions_for(candles, args, info)
    notional = args.notional or prop.suggest_notional(candles, contract)
    qtys = ([int(q) for q in args.qty_sweep.split(",")] if args.qty_sweep
            else [q for q in (1, 2, 3, 4, 6, 8) if q <= rules.max_contracts])
    sweep = prop.sweep_size(candles, pos, rules, contract, qtys,
                            stride=args.stride, max_bars=args.max_bars,
                            enforce_overnight=not args.allow_overnight,
                            notional=notional)
    sweep["rules_detail"] = {
        "label": rules.label, "target": rules.profit_target,
        "drawdown": rules.max_drawdown, "trail_mode": rules.trail_mode,
        "daily_loss_limit": rules.daily_loss_limit, "as_of": rules.as_of,
        "notional": notional, "contract": contract.symbol,
        "overnight_enforced": not args.allow_overnight,
    }
    report["prop"] = sweep
    return sweep


def cmd_heatmap(cfg, days, limit, args):
    """Build every edge map and write the HTML report."""
    report = _analyze_edge(cfg, days, limit, args)
    ana.print_edge_table(report)
    if args.prop:
        sweep = _attach_prop(report, cfg, days, limit, args)
        prop.print_size_sweep(sweep)
    out = args.out or "krypt_heatmaps.html"
    with open(out, "w") as f:
        f.write(heatmapmod.render(report))
    print(f"\n>> wrote {out} — heatmaps for edge-over-time, regime, cost "
          f"sensitivity,\n   session, correlation, position overlap and "
          f"parameter robustness.")
    keep = nj.recommend(report, top=3)
    if keep:
        print(f">> candidates worth a second date range: {', '.join(keep)}")
        print("   export them to NinjaTrader with: python3 -m krypt.app ninja "
              f"--days {days or 21}")
    else:
        print(">> nothing on this sample clears the bar. That IS the result — "
              "no strategy\n   here is worth risking money on this data.")


def cmd_ninja(cfg, days, limit, args):
    """Generate NinjaScript (.cs) for the strategies that still show an edge."""
    report = None
    if args.no_analysis:
        names = [args.strategy] if args.strategy else list(stratlib.REGISTRY)
        print("Skipping analysis (--no-analysis): headers will carry no evidence.")
    else:
        report = _analyze_edge(cfg, days, limit, args)
        ana.print_edge_table(report)
        names = [args.strategy] if args.strategy else nj.recommend(report, top=args.top)
        if not names:
            print("\nNo strategy cleared the bar on this sample, so nothing was "
                  "exported.\n(Use --strategy NAME to export one anyway, with its "
                  "real numbers in the header.)")
            return
    known = (list(eqlib.ALL) if args.strats == "equity" else list(stratlib.REGISTRY))
    unknown = [n for n in names if n not in known]
    if unknown:
        print(f"unknown strategy: {', '.join(unknown)}", file=sys.stderr)
        sys.exit(1)
    rules = prop.load_rules(args.firm) if args.prop else None
    skipped = []
    ok_names = []
    for n in names:
        try:
            nj.generate(n, report, rules=rules)
            ok_names.append(n)
        except KeyError as e:
            skipped.append((n, str(e).strip('"\'')))
    paths = nj.export(ok_names, report, outdir=args.outdir, quantity=args.quantity,
                      allow_short=not args.long_only, rules=rules)
    for n, why in skipped:
        print(f"  skipped {n}: {why}")
    print(f"\n>> wrote {len(paths)} file(s) to ./{args.outdir}/")
    for p in paths:
        print("   ", p)
    print("   Copy the .cs files to Documents\\NinjaTrader 8\\bin\\Custom\\Strategies\\, "
          "press F5 in the\n   NinjaScript editor, then backtest them in Strategy "
          "Analyzer on YOUR instrument\n   and YOUR costs before going anywhere near "
          "a live account.")


def _positions_for(candles, args, info):
    """(cache, registry, positions-by-strategy) for whichever library is selected."""
    if args.strats == "equity":
        cache = eqlib.precompute(candles, (info or {}).get("extras"))
        registry, warmup, allow_short = eqlib.registry(cache), 300, False
    else:
        cache = stratlib.precompute(candles)
        registry, warmup, allow_short = dict(stratlib.REGISTRY), 60, not args.long_only
    pos = {name: ana.position_series(cache, fn, warmup=warmup, allow_short=allow_short)
           for name, fn in registry.items()}
    return cache, registry, pos


def cmd_prop(cfg, days, limit, args):
    """Would these strategies pass an Apex / Topstep evaluation?"""
    candles = _load_candles(cfg, days, limit, args)
    info = _load_candles.last_info
    rules = prop.load_rules(args.firm)
    contract = prop.CONTRACTS.get(args.contract.upper())
    if contract is None:
        print(f"unknown contract '{args.contract}'. choices: "
              f"{', '.join(sorted(prop.CONTRACTS))}", file=sys.stderr)
        sys.exit(1)
    _, registry, pos = _positions_for(candles, args, info)
    notional = args.notional or prop.suggest_notional(candles, contract)
    if notional and not args.notional:
        px = candles[len(candles) // 2]["close"]
        print(f"  price series (~{px:,.2f}) is not this contract's own price — "
              f"treating it as a PROXY at ${notional:,.0f} notional per contract.\n"
              f"  Override with --notional. Results are an approximation of "
              f"trading {contract.symbol}, not a simulation of it.")

    rows = []
    for name in registry:
        cohort = prop.evaluate_cohorts(
            candles, pos[name], rules, contract, qty=args.qty,
            stride=args.stride, max_bars=args.max_bars,
            enforce_overnight=not args.allow_overnight, notional=notional)
        rows.append({"strategy": name, "cohort": cohort})
    rows.sort(key=lambda r: r["cohort"]["pass_rate"], reverse=True)
    prop.print_report(rows, rules, contract, args.qty, notional)

    if not args.no_size_sweep:
        qtys = [int(q) for q in args.qty_sweep.split(",")] if args.qty_sweep else \
            [q for q in (1, 2, 3, 4, 6, 8) if q <= rules.max_contracts]
        sweep = prop.sweep_size(candles, pos, rules, contract, qtys,
                                stride=args.stride, max_bars=args.max_bars,
                                enforce_overnight=not args.allow_overnight,
                                notional=notional)
        prop.print_size_sweep(sweep)
        best = max(((r["strategy"], c["qty"], cell["pass_pct"])
                    for r, cells in ((r, r["cells"]) for r in sweep["rows"])
                    for c, cell in zip(sweep["cols"], cells)),
                   key=lambda t: t[2], default=None)
        if best and best[2] > 0:
            print(f"  best combination on this sample: {best[0]} at {best[1]} "
                  f"contract(s) — {best[2]:.0f}% of evaluations passed. "
                  f"{100 - best[2]:.0f}% failed.")

    bar_secs = ana.median_bar_secs(candles)
    if bar_secs >= 86_400 and not args.allow_overnight:
        print("\n  NOTE: these are DAILY bars, and every one of these strategies "
              "holds\n  positions overnight — which Apex and Topstep both forbid "
              "outright. The\n  failures above are that rule, not the strategies' "
              "P&L. Prop evaluation is\n  an INTRADAY game: re-run this on "
              "minute/hourly bars with rules that flatten\n  before the session "
              "close, or use --allow-overnight to see the counterfactual\n  "
              "(informative, but not a result you can trade at a prop firm).")


def cmd_discover(cfg, days, limit, args):
    """Search -> sweep -> hybrids -> proof, in one pass.

    The pipeline is deliberately ordered so each stage cannot cheat the next:
    parameters are chosen IN-SAMPLE, hybrids are built from those choices, and
    everything is judged out-of-sample and against a multiple-testing correction
    that counts every hypothesis the run explored.
    """
    candles = _load_candles(cfg, days, limit, args)
    info = _load_candles.last_info
    cache, registry, base_pos = _positions_for(candles, args, info)
    warmup = 300 if args.strats == "equity" else 60
    bpy = ana.empirical_bars_per_year(candles, (info or {}).get("interval") or cfg.interval)
    cost = (args.fee_bps + args.slippage_bps) / 1e4
    A = ana.Analysis(args.symbols or "series", (info or {}).get("interval") or cfg.interval,
                     candles, registry, windows=args.windows, fee_bps=args.fee_bps,
                     slippage_bps=args.slippage_bps, allow_short=False,
                     warmup=warmup, cache=cache, extras=(info or {}).get("extras"),
                     oos_frac=args.oos_frac)
    split = A._split()
    hypotheses = 0

    # ---- 1. sweep every rule's grid, and pick each rule's best cell IN-SAMPLE
    specs = (eqlib.sweep_specs(cache) if args.strats == "equity" else {})
    tuned, sweeps = {}, []
    if specs:
        print(f"Sweeping {len(specs)} parameter grids (in-sample selection) ...")
        for name, spec in specs.items():
            grid = A.sweep_grid(name, spec, in_sample_only=True)
            sweeps.append(grid)
            best, best_key = None, None
            for row in grid["rows"]:
                for cell in row["cells"]:
                    hypotheses += 1
                    if best is None or cell["ann_sharpe"] > best:
                        best, best_key = cell["ann_sharpe"], cell["params"]
            if best_key is not None:
                tuned[f"{name}*"] = spec["make"](cache, best_key["x"], best_key["y"])
                print(f"  {name:<20} best in-sample cell: "
                      f"{best_key['x']}/{best_key['y']}  (Sharpe {best})")

    # ---- 2. hybrids from the tuned rules
    pool = dict(base_pos)
    pool.update(tuned)
    print(f"\nCombining {len(tuned) or len(base_pos)} rules into hybrids ...")
    source = tuned or base_pos
    rows, matrices = hyb.search_pairs(cache, source, bpy=bpy, warmup=warmup,
                                      split=split, fee_bps=args.fee_bps,
                                      slippage_bps=args.slippage_bps)
    votes = hyb.search_votes(cache, source, bpy=bpy, warmup=warmup, split=split,
                             fee_bps=args.fee_bps, slippage_bps=args.slippage_bps)
    rows.extend(votes)
    hypotheses += len(rows)

    bench_pos = [1] * len(candles)
    bench = hyb.evaluate_positions(cache, bench_pos, bpy=bpy, warmup=warmup,
                                   split=split, fee_bps=args.fee_bps,
                                   slippage_bps=args.slippage_bps)
    hyb.print_hybrids(rows, benchmark=bench, top=args.top)

    # ---- 3. portfolios of whatever ranked best out-of-sample
    singles = {n: hyb.evaluate_positions(cache, p, bpy=bpy, warmup=warmup,
                                         split=split, fee_bps=args.fee_bps,
                                         slippage_bps=args.slippage_bps)
               for n, p in source.items()}
    ranked = sorted(singles, key=lambda n: singles[n]["oos_sharpe"], reverse=True)
    ports = hyb.build_portfolios(cache, source, ranked, bpy=bpy, warmup=warmup,
                                 cost=cost)
    if ports:
        print("\n  equal-weight portfolios (capital split, daily rebalance — "
              "combines equity curves, not signals):")
        for pf in ports:
            print(f"    {pf['name']:<22} return {pf['return_pct']:>8.1f}%  "
                  f"Sharpe {pf['full_sharpe']:>5.2f}  maxDD {pf['max_dd_pct']:>5.1f}%"
                  f"   [{', '.join(pf['members'])}]")

    # ---- 4. the gauntlet, on a shortlist, corrected for everything explored
    # Base rules AND their tuned variants go in: the gap between them is the
    # clearest read on how much of a "discovery" is just parameter selection.
    shortlist = dict(base_pos)
    shortlist.update(source)
    for r in rows[:args.top]:
        shortlist[r["name"]] = r["positions"]
    shortlist["buy_hold"] = bench_pos
    res = val.gauntlet(candles, shortlist, bpy=bpy, warmup=warmup, cost=cost,
                       samples=args.permutations, benchmark=bench["full_sharpe"],
                       m_total=hypotheses, oos_start=split)
    val.print_gauntlet(res, top=args.top)

    survivors = [r for r in res["rows"] if r["verdict"].startswith("SURVIVES")]
    print("\n=== WHAT TO TAKE TO NINJATRADER ===")
    if survivors:
        # Eleven survivors that all contain the same rule are one finding with
        # eleven names. Say so, or the count reads as eleven independent edges.
        counts = {}
        for r in survivors:
            for m in (r["name"].replace(" SWITCH ", " ").replace(" AND ", " ")
                      .replace(" OR ", " ").split()):
                counts[m] = counts.get(m, 0) + 1
        common = [m for m, c in counts.items() if c >= max(2, 0.8 * len(survivors))]
        if common and len(survivors) > 1:
            print(f"  READ THIS FIRST: {len(survivors)} survivors, but every one "
                  f"contains {', '.join(common)} —\n  that is ONE finding wearing "
                  f"{len(survivors)} names. The combinations around it mostly "
                  f"change\n  exposure, not edge.")
        for r in survivors[:5]:
            print(f"  {r['name']}  (Sharpe {r['close_sharpe']} close / "
                  f"{r['delayed_sharpe']} next-open, p_adj {r['p_adjusted']}, "
                  f"exposure {r['exposure']*100:.0f}%)")
        base_only = [r for r in survivors if r["name"] in base_pos]
        if not base_only:
            print("  NOTE: no UNTUNED rule survived — only parameter-tuned variants "
                  "did. Tuning\n  on this sample and testing on the same sample is "
                  "exactly what the correction\n  is trying to catch, so treat "
                  "these as candidates for a fresh date range,\n  not as proven.")
        print("  Export with: python3 -m krypt.app ninja --strategy <name> "
              f"--strats {args.strats} --firm {args.firm} --prop")
    else:
        best = min(res["rows"], key=lambda r: (r["p_adjusted"], -r["close_sharpe"]))
        print(f"  Nothing cleared the bar after correcting for "
              f"{res['hypotheses']} explored hypotheses.")
        print(f"  Closest: {best['name']} — raw p {best['p_value']:.5f}, adjusted "
              f"{best['p_adjusted']} (needs <= {res['alpha']}),\n"
              f"  held-out-tail p {best.get('p_oos')}, Sharpe "
              f"{best['close_sharpe']} close / {best['delayed_sharpe']} next-open "
              f"at {best['exposure']*100:.0f}% exposure\n"
              f"  vs buy & hold {bench['full_sharpe']}.")
        print("  That is a CANDIDATE, not an edge. The way it becomes one is a "
              "different symbol\n  or date range, where it is a single hypothesis "
              "instead of one of hundreds and\n  the same p-value would be "
              "conclusive. Re-run: same command, different --file.")
    report = A.run_all()
    report["sweeps"] = sweeps or report.get("sweeps")
    report["hybrids"] = {"rows": [{k: v for k, v in r.items() if k != "positions"}
                                  for r in rows[:args.top]],
                         "matrices": matrices, "benchmark": bench,
                         "portfolios": ports}
    report["validation"] = res
    out = args.out or "krypt_discover.html"
    with open(out, "w") as f:
        f.write(heatmapmod.render(report))
    print(f"\n>> wrote {out}")
    return report


def cmd_live(cfg, exchange):
    """Write a standalone live-tick dashboard (browser connects to exchange WS)."""
    sym = cfg.symbols[0]
    html = live_dashboard.render(sym, exchange)
    with open("krypt_live.html", "w") as f:
        f.write(html)
    print(f"✓ wrote krypt_live.html — open it in a browser to stream live {sym} "
          f"ticks from {exchange} (no key, no backend).")


def main(argv=None):
    p = argparse.ArgumentParser(prog="krypt", description="Advanced crypto trader")
    p.add_argument("command",
                   choices=["analyze", "serve", "backtest", "compare", "heatmap",
                            "discover", "prop", "ninja", "live", "download"])
    p.add_argument("--mode", choices=["analyze", "paper", "live"])
    p.add_argument("--symbols")
    p.add_argument("--interval")
    p.add_argument("--strategy",
                   help="serve/analyze: scalper|market_maker|hedge|trend; "
                        "ninja: one of the 10 backtest strategies")
    p.add_argument("--venue", choices=["binance", "coinbase"], help="execution venue")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--exchange", choices=["binance", "coinbase"], default="binance")
    p.add_argument("--days", type=int, help="history window (download/backtest)")
    p.add_argument("--kind", choices=["klines", "aggTrades"], default="klines")
    p.add_argument("--fee-bps", type=float, default=10.0, help="per-side taker fee bps")
    p.add_argument("--slippage-bps", type=float, default=2.0)
    p.add_argument("--no-compound", action="store_true", help="disable equity compounding")
    p.add_argument("--leverage", type=float, default=1.0, help="leverage for compare/backtest")
    p.add_argument("--windows", type=int, default=12,
                   help="time slices for the edge-decay heatmap")
    p.add_argument("--oos-frac", type=float, default=0.3,
                   help="fraction of the sample held out of sample (heatmap/ninja)")
    p.add_argument("--long-only", action="store_true",
                   help="no shorts (spot accounts)")
    p.add_argument("--out", help="output file for the heatmap report")
    p.add_argument("--outdir", default="ninja", help="output dir for NinjaScript")
    p.add_argument("--top", type=int, default=3,
                   help="how many ranked strategies to export to NinjaScript")
    p.add_argument("--quantity", type=int, default=1,
                   help="default order quantity in generated NinjaScript")
    p.add_argument("--source", choices=["binance", "csv", "kaggle"],
                   default="binance", help="where candles come from")
    p.add_argument("--file", help="CSV file or folder (--source csv)")
    p.add_argument("--dataset", help="Kaggle dataset handle (--source kaggle)")
    p.add_argument("--strats", choices=["crypto", "equity"], default="crypto",
                   help="strategy library: crypto minutes or US equity dailies")
    p.add_argument("--firm", default="apex-50k",
                   help="prop rules: preset name or path to a rules JSON "
                        f"({', '.join(sorted(prop.PRESETS))})")
    p.add_argument("--contract", default="MNQ",
                   help=f"futures contract ({', '.join(sorted(prop.CONTRACTS))})")
    p.add_argument("--qty", type=int, default=1, help="contracts per trade")
    p.add_argument("--notional", type=float,
                   help="$ notional per contract when the price series is a proxy")
    p.add_argument("--stride", type=int, default=21,
                   help="bars between evaluation start dates (cohorts)")
    p.add_argument("--max-bars", type=int,
                   help="cap on bars per evaluation attempt")
    p.add_argument("--qty-sweep", help="comma-separated contract sizes to sweep")
    p.add_argument("--no-size-sweep", action="store_true",
                   help="skip the pass-rate-by-size sweep")
    p.add_argument("--allow-overnight", action="store_true",
                   help="ignore the no-overnight rule (counterfactual only)")
    p.add_argument("--permutations", type=int, default=200,
                   help="permutation samples per candidate in the edge test")
    p.add_argument("--prop", action="store_true",
                   help="bake the --firm rules into the generated NinjaScript")
    p.add_argument("--no-analysis", action="store_true",
                   help="emit NinjaScript templates without measuring anything")
    args = p.parse_args(argv)

    # --strategy is overloaded: live strategies for analyze/serve, backtest
    # strategies for ninja. Validate against the right registry instead of
    # letting it blow up several layers down.
    if args.strategy:
        live_cmds = ("analyze", "serve")
        valid = (list(LIVE_STRATEGIES) if args.command in live_cmds
                 else (list(eqlib.ALL) if args.strats == "equity"
                       else list(stratlib.REGISTRY)))
        if args.strategy not in valid:
            print(f"SAFETY: unknown --strategy '{args.strategy}' for command "
                  f"'{args.command}'. choices: {', '.join(valid)}", file=sys.stderr)
            sys.exit(2)

    cfg = load_config(mode=args.mode, symbols=args.symbols,
                      interval=args.interval,
                      strategy=args.strategy if args.command in ("analyze", "serve") else None,
                      venue=args.venue)
    try:
        cfg.assert_live_allowed()
    except PermissionError as e:
        print("SAFETY:", e, file=sys.stderr)
        sys.exit(2)

    try:
        if args.command == "analyze":
            cmd_analyze(cfg)
        elif args.command == "serve":
            cmd_serve(cfg, args.port)
        elif args.command == "backtest":
            cmd_backtest(cfg, args.days, args.limit, args.fee_bps,
                         args.slippage_bps, args.no_compound)
        elif args.command == "compare":
            cmd_compare(cfg, args.days, args.limit, args.leverage,
                        args.fee_bps, args.slippage_bps, args.no_compound)
        elif args.command == "discover":
            cmd_discover(cfg, args.days, args.limit, args)
        elif args.command == "prop":
            cmd_prop(cfg, args.days, args.limit, args)
        elif args.command == "heatmap":
            cmd_heatmap(cfg, args.days, args.limit, args)
        elif args.command == "ninja":
            cmd_ninja(cfg, args.days, args.limit, args)
        elif args.command == "download":
            cmd_download(cfg, args.days or 21, args.kind)
        elif args.command == "live":
            cmd_live(cfg, args.exchange)
    except BinanceError as e:
        print(f"\nDATA ERROR: {e}\n"
              "If Binance is blocked on this network (sandbox/firewall), run KRYPT\n"
              "on a machine with outbound access to api.binance.com.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
