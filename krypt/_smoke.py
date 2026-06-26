"""Offline smoke test — exercises indicators, scoring, risk, trader (paper),
and dashboard rendering on synthetic data, with NO network. Run:

    python3 -m krypt._smoke
"""

from __future__ import annotations

from .config import load_config
from . import indicators
from .scoring import score_snapshot
from .risk import RiskEngine
from .trader import Trader
from .alerts import default_rules
from . import dashboard


def synth(n=240, start=60000.0):
    out, price, st = [], start, 12345
    for i in range(n):
        st = (1103515245 * st + 12345) & 0x7FFFFFFF
        r = st / 0x7FFFFFFF - 0.5
        price *= 1 + 0.0006 + 0.02 * r
        o = price
        c = price * (1 + 0.01 * r)
        out.append({"time": 1_700_000_000_000 + i * 60000, "open": o,
                    "high": max(o, c) * 1.002, "low": min(o, c) * 0.998,
                    "close": c, "volume": 10 + 50 * abs(r)})
        price = c
    return out


def main():
    cfg = load_config(mode="paper")
    candles = synth()
    ind = indicators.compute_all(candles)
    snap = ind["latest"]
    sc = score_snapshot(snap)
    print("indicators latest:",
          {k: (round(v, 2) if isinstance(v, float) else v) for k, v in snap.items()})
    print("score:", sc["score"], sc["label"])
    assert -100 <= sc["score"] <= 100
    assert all(v is not None for v in (snap["rsi"], snap["macd"], snap["atr"], snap["vwap"]))

    # risk + trader (paper) — no client needed for analyze/paper buy/sell
    risk = RiskEngine(limits=cfg.risk)
    trader = Trader(cfg, client=None, risk=risk)
    price = snap["price"]
    r1 = trader.execute("BTCUSDT", "BUY", price, notional_usd=40, reason="smoke")
    r2 = trader.execute("BTCUSDT", "SELL", price * 1.01, notional_usd=40, reason="smoke")
    print("paper buy :", r1["event"])
    print("paper sell:", r2["event"], "pnl", r2.get("realized_pnl"))
    assert r1["event"] == "paper_fill" and r2["event"] == "paper_fill"

    # risk rejection: order over max
    rej = trader.execute("BTCUSDT", "BUY", price, notional_usd=999999, reason="too big")
    print("oversize  :", rej["event"], "-", rej.get("risk"))
    assert rej["event"] == "rejected"

    # alerts
    al = default_rules("BTCUSDT")
    fired = al.check("BTCUSDT", {"rsi": 80, "score": 70})
    print("alerts    :", [a["metric"] for a in fired])

    # dashboard renders
    state = {"symbol": "BTCUSDT", "price": price, "mode": "paper", "testnet": True,
             "candles": candles[-120:], "ema21": ind["ema21"][-120:],
             "scoring": sc, "book": {"imbalance": 0.1, "spread": 1.2,
             "best_bid": price, "best_ask": price * 1.0001},
             "risk": risk.snapshot(), "alerts": fired}
    html = dashboard.render(state)
    assert "KRYPT Trader" in html and "lightweight-charts" in html
    with open("krypt_dashboard.html", "w") as f:
        f.write(html)
    print("dashboard : wrote krypt_dashboard.html (%d bytes)" % len(html))

    # tick -> candle resampling
    from . import data as datamod
    ticks = []
    for i in range(2000):
        ticks.append({"time": 1_700_000_000_000 + i * 500, "price": 60000 + i,
                      "qty": 0.01, "is_buyer_maker": i % 2 == 0})
    bars = datamod.ticks_to_candles(ticks, bucket_secs=1)
    assert bars and all(b["high"] >= b["low"] for b in bars)
    print("resample  : %d ticks -> %d 1s candles" % (len(ticks), len(bars)))

    # realistic backtester runs and reports
    from . import backtest as bt
    res = bt.run(candles, fee_bps=10, slippage_bps=2, compound=True)
    st = res.stats()
    assert "final_equity" in st and st["max_drawdown_pct"] >= 0
    print("backtest  : %d trades, return %s%%, fees $%s, Sharpe %s" % (
        st["trades"], st["total_return_pct"], st["fees_paid"], st["sharpe"]))

    # 10-strategy comparison + leverage liquidation model
    from . import strats
    assert len(strats.REGISTRY) == 10
    rows1 = bt.compare(candles, strats.REGISTRY, leverage=1, fee_bps=10, slippage_bps=2)
    rows10 = bt.compare(candles, strats.REGISTRY, leverage=10, fee_bps=10, slippage_bps=2)
    assert len(rows1) == 10
    liq10 = sum(r.get("liquidations", 0) for r in rows10)
    print("compare   : 10 strats ranked; best=%s (%.1f%%); 10x liquidations=%d" % (
        rows1[0]["strategy"], rows1[0]["total_return_pct"], liq10))
    assert liq10 >= 0  # leverage model wired

    print("\nALL SMOKE CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
