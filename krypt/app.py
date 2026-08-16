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


def cmd_heatmap(cfg, days, limit, args):
    """Build every edge map and write the HTML report."""
    report = _analyze_edge(cfg, days, limit, args)
    ana.print_edge_table(report)
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
    unknown = [n for n in names if n not in stratlib.REGISTRY]
    if unknown:
        print(f"unknown strategy: {', '.join(unknown)}", file=sys.stderr)
        sys.exit(1)
    paths = nj.export(names, report, outdir=args.outdir, quantity=args.quantity,
                      allow_short=not args.long_only)
    print(f"\n>> wrote {len(paths)} file(s) to ./{args.outdir}/")
    for p in paths:
        print("   ", p)
    print("   Copy the .cs files to Documents\\NinjaTrader 8\\bin\\Custom\\Strategies\\, "
          "press F5 in the\n   NinjaScript editor, then backtest them in Strategy "
          "Analyzer on YOUR instrument\n   and YOUR costs before going anywhere near "
          "a live account.")


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
                            "ninja", "live", "download"])
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
    p.add_argument("--no-analysis", action="store_true",
                   help="emit NinjaScript templates without measuring anything")
    args = p.parse_args(argv)

    # --strategy is overloaded: live strategies for analyze/serve, backtest
    # strategies for ninja. Validate against the right registry instead of
    # letting it blow up several layers down.
    if args.strategy:
        live_cmds = ("analyze", "serve")
        valid = (list(LIVE_STRATEGIES) if args.command in live_cmds
                 else list(stratlib.REGISTRY))
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
