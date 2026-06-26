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
from . import dashboard, indicators
from .scoring import score_snapshot


def build(cfg):
    client = BinanceClient(cfg)
    risk = RiskEngine(limits=cfg.risk)
    trader = Trader(cfg, client, risk)
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


def cmd_backtest(cfg, limit):
    """Simple long/flat backtest of the scoring engine on historical candles."""
    client = BinanceClient(cfg)
    sym = cfg.symbols[0]
    candles = client.klines(sym, cfg.interval, min(limit, 1000))
    equity, pos_qty, entry = 1000.0, 0.0, 0.0
    trades, wins = 0, 0
    peak, max_dd = equity, 0.0
    for i in range(60, len(candles)):
        window = candles[:i + 1]
        snap = indicators.compute_all(window)["latest"]
        sc = score_snapshot(snap)["score"]
        price = window[-1]["close"]
        if pos_qty == 0 and sc >= 35:
            pos_qty = equity / price
            entry = price
        elif pos_qty > 0 and sc <= -10:
            pnl = (price - entry) * pos_qty
            equity += pnl
            trades += 1
            wins += 1 if pnl > 0 else 0
            pos_qty = 0.0
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak * 100)
    if pos_qty > 0:
        equity += (candles[-1]["close"] - entry) * pos_qty
    print(f"Backtest {sym} {cfg.interval} ({len(candles)} candles)")
    print(f"  final equity : ${equity:,.2f}  ({(equity/1000-1)*100:+.1f}%)")
    print(f"  trades       : {trades}  win rate {100*wins/trades if trades else 0:.0f}%")
    print(f"  max drawdown : {max_dd:.1f}%")
    print("  (toy backtest, no fees/slippage — do not trust it with real money)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="krypt", description="Advanced crypto trader")
    p.add_argument("command", choices=["analyze", "serve", "backtest"])
    p.add_argument("--mode", choices=["analyze", "paper", "live"])
    p.add_argument("--symbols")
    p.add_argument("--interval")
    p.add_argument("--strategy", choices=["scalper", "market_maker", "hedge", "trend"])
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--limit", type=int, default=500)
    args = p.parse_args(argv)

    cfg = load_config(mode=args.mode, symbols=args.symbols,
                      interval=args.interval, strategy=args.strategy)
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
            cmd_backtest(cfg, args.limit)
    except BinanceError as e:
        print(f"\nDATA ERROR: {e}\n"
              "If Binance is blocked on this network (sandbox/firewall), run KRYPT\n"
              "on a machine with outbound access to api.binance.com.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
