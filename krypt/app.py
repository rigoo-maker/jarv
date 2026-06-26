"""KRYPT CLI entry point.

Subcommands:
  analyze   one-shot read-only snapshot -> writes krypt_dashboard.html
  serve     live dashboard at http://localhost:PORT (auto-refresh) + trading loop
  backtest  run the trend strategy over historical candles, report stats

Mode (analyze|paper|live) and all risk limits come from env / config.py.
LIVE requires the two-lock guard (see config.assert_live_allowed).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import load_config
from .binance_client import BinanceClient, BinanceError
from .risk import RiskEngine
from .trader import Trader
from .alerts import default_rules
from .strategies import make_strategy
from .engine import Engine
from . import dashboard, live_dashboard, indicators, data as datamod, backtest as bt
from . import strats as stratlib
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


def _load_candles(cfg, days, limit):
    sym = cfg.symbols[0]
    if days:
        end = date.today() - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        print(f"Loading {sym} {cfg.interval} {start}..{end} (data.binance.vision)")
        return datamod.download_klines_range(sym, cfg.interval, start, end)
    return BinanceClient(cfg).klines(sym, cfg.interval, min(limit, 1000))


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
                   choices=["analyze", "serve", "backtest", "compare", "live", "download"])
    p.add_argument("--mode", choices=["analyze", "paper", "live"])
    p.add_argument("--symbols")
    p.add_argument("--interval")
    p.add_argument("--strategy", choices=["scalper", "market_maker", "hedge", "trend"])
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
    args = p.parse_args(argv)

    cfg = load_config(mode=args.mode, symbols=args.symbols,
                      interval=args.interval, strategy=args.strategy, venue=args.venue)
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
