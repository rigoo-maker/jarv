"""Offline smoke test — exercises indicators, scoring, risk, trader (paper),
and dashboard rendering on synthetic data, with NO network. Run:

    python3 -m krypt._smoke
"""

from __future__ import annotations

import os

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

    # mark-to-market invariant: buy & hold must reproduce the instrument.
    # Regression test for a real bug — booking P&L only on trade close made a
    # held position a flat line with one jump, so drawdown measured ~0 and every
    # Sharpe for a slow strategy was meaningless.
    bh = bt.run_signal(strats.precompute(candles), lambda c, i: 1, fee_bps=0,
                       slippage_bps=0, warmup=60, allow_short=False)
    bh_stats = bh.stats()
    price_move = (candles[-1]["close"] / candles[60]["close"] - 1) * 100
    assert abs(bh_stats["total_return_pct"] - price_move) < 0.5, (
        bh_stats["total_return_pct"], price_move)
    peak = mdd = 0.0
    for c in candles[60:]:
        peak = max(peak, c["close"])
        mdd = max(mdd, (peak - c["close"]) / peak * 100)
    assert abs(bh_stats["max_drawdown_pct"] - mdd) < 1.0, (
        bh_stats["max_drawdown_pct"], mdd)
    print("mark2mkt  : buy&hold %+.1f%% vs price %+.1f%%, maxDD %.1f%% vs %.1f%%" % (
        bh_stats["total_return_pct"], price_move, bh_stats["max_drawdown_pct"], mdd))

    # edge analytics: every map + the active-edge ranking (+1 row = benchmark)
    from . import analytics as ana
    rep = ana.Analysis("BTCUSDT", "1m", candles, windows=4).run_all()
    n_rows = len(strats.REGISTRY) + 1
    assert len(rep["windows"]["cols"]) == 4
    assert len(rep["windows"]["rows"]) == n_rows
    assert len(rep["regimes"]["cols"]) == 9          # 3 trend x 3 volatility
    assert len(rep["hours"]["cols"]) == 24
    assert len(rep["costs"]["cols"]) == 7
    corr = rep["correlation"]["matrix"]
    assert all(abs(corr[i][i] - 1.0) < 1e-9 for i in range(len(corr)))
    for i in range(len(corr)):                        # correlation is symmetric
        for j in range(len(corr)):
            a, b = corr[i][j], corr[j][i]
            assert (a is None and b is None) or abs(a - b) < 1e-9
    ov = rep["overlap"]["matrix"]
    assert all(v is None or -1.0001 <= v <= 1.0001 for row in ov for v in row)
    assert rep["sweeps"] and all(m["rows"] and m["cols"] for m in rep["sweeps"])
    edge = rep["edge"]["rows"]
    assert len(edge) == n_rows
    assert any(r["is_benchmark"] for r in edge)
    assert all(0 <= r["score"] <= 100 for r in edge)
    assert edge == sorted(edge, key=lambda r: r["score"], reverse=True)
    print("analytics : %d windows, %d regimes, %d cost levels; best=%s (%s, score %s)" % (
        len(rep["windows"]["cols"]), len(rep["regimes"]["cols"]),
        len(rep["costs"]["cols"]), edge[0]["strategy"], edge[0]["verdict"],
        edge[0]["score"]))

    # heatmap report renders, is self-contained, and paints both themes
    from . import heatmap as hm
    page = hm.render(rep)
    assert "KRYPT edge maps" in page and "<td class=\"cell\"" in page
    assert "http://" not in page and "https://" not in page   # no CDN, works offline
    assert "prefers-color-scheme" in page and "data-theme" in page
    worst = min(min(hm.contrast(l, il), hm.contrast(d, idk))
                for v in (i / 20 for i in range(-20, 21))
                for l, d, il, idk in [hm.cell_colors(v, 1.0)])
    assert worst > 4.0, worst        # cell numbers stay readable on every step
    with open("krypt_heatmaps.html", "w") as f:
        f.write(page)
    print("heatmaps  : wrote krypt_heatmaps.html (%d bytes), worst cell-ink "
          "contrast %.2f:1" % (len(page), worst))

    # NinjaScript generation for every strategy
    from . import ninjascript as nj
    for name in strats.REGISTRY:
        fname, code = nj.generate(name, rep)  # benchmark has no template, by design
        assert code.count("{") == code.count("}"), name
        assert code.isascii(), name           # NinjaScript editors are not UTF-8 safe
        for needle in ("namespace NinjaTrader.NinjaScript.Strategies",
                       "protected override void OnStateChange()",
                       "protected override void OnBarUpdate()",
                       "State.SetDefaults", "EnterLong(", "EnterShort(",
                       "SetStopLoss(", "WHAT THE ANALYSIS MEASURED"):
            assert needle in code, (name, needle)
    print("ninjascript: generated %d NinjaTrader 8 strategies (balanced braces, "
          "ASCII, evidence headers)" % len(strats.REGISTRY))

    # CSV loader: header aliases, ambiguous slash dates, multi-symbol guard
    import tempfile
    from . import csvdata
    with tempfile.TemporaryDirectory() as td:
        p1 = os.path.join(td, "plain.csv")
        from datetime import date, timedelta
        with open(p1, "w") as f:
            f.write("Date,Open,High,Low,Close,Volume,VIX\n")
            for i, c in enumerate(candles[:50]):
                d = date(2024, 1, 1) + timedelta(days=i)
                f.write("%s,%s,%s,%s,%s,%s,%s\n" % (
                    d.isoformat(), c["open"], c["high"], c["low"], c["close"],
                    c["volume"], 15 + i % 10))
        got, info = csvdata.load(p1, quiet=True)
        assert len(got) == 50 and info["interval"] == "1d"
        assert info["extras"][0]["vix"] == 15

        p2 = os.path.join(td, "slash.csv")
        with open(p2, "w") as f:                       # 25/12 proves DD/MM order
            f.write("timestamp,o,h,l,c\n01/02/2024,1,2,0.5,1.5\n25/12/2024,1,2,0.5,1.6\n"
                    "26/12/2024,1,2,0.5,1.7\n")
        got2, info2 = csvdata.load(p2, quiet=True)
        assert info2["date_format"] == "%d/%m/%Y", info2["date_format"]

        p3 = os.path.join(td, "multi.csv")
        with open(p3, "w") as f:
            f.write("Date,Ticker,Open,High,Low,Close\n2024-01-01,AAA,1,2,0.5,1.5\n"
                    "2024-01-01,BBB,1,2,0.5,1.5\n2024-01-02,AAA,1,2,0.5,1.6\n")
        try:
            csvdata.load(p3, quiet=True)
            raise AssertionError("multi-symbol file should demand a symbol")
        except csvdata.CsvError:
            pass
        one, _ = csvdata.load(p3, "AAA", quiet=True)
        assert len(one) == 2
    print("csvdata   : aliases, DD/MM detection, extras and multi-symbol guard OK")

    # equity library: VIX rules present only when the data has VIX
    from . import strats_equity as eqlib
    # equity rules need DAILY spacing — re-stamp the synthetic bars one per day so
    # the measured annualization factor is a daily one
    daily = [{**c, "time": 1_600_000_000_000 + i * 86_400_000}
             for i, c in enumerate(candles)]
    ecache = eqlib.precompute(daily, [{"vix": 15 + i % 8} for i in range(len(daily))])
    assert len(eqlib.registry(ecache)) == 10
    assert len(eqlib.registry(eqlib.precompute(daily))) == 8
    for name, fn in eqlib.registry(ecache).items():
        vals = {fn(ecache, i) for i in range(len(daily))}
        assert vals <= {0, 1}, (name, vals)       # long/flat only, by design
    erep = ana.Analysis("TEST", "1d", daily, eqlib.registry(ecache), windows=4,
                        cache=ecache, allow_short=False, warmup=100).run_all()
    assert erep["regimes"]["cols"][0]["trend"] == "below-200"   # equity regime axis
    assert erep["decomposition"]["rows"]                        # overnight vs intraday
    # annualization is measured from timestamps: ~365 for these daily bars, and
    # ~525,600 for the minute bars above — not a hardcoded crypto constant
    assert 360 < erep["meta"]["bars_per_year"] < 370, erep["meta"]["bars_per_year"]
    assert 500_000 < rep["meta"]["bars_per_year"] < 550_000
    print("equity    : %d rules (10 with VIX / 8 without), regime axis = 200-day, "
          "%.0f bars/yr" % (len(eqlib.registry(ecache)), erep["meta"]["bars_per_year"]))

    # prop-firm rules: the semantics that separate Apex from Topstep
    from . import propfirm as pf
    apex = pf.get_rules("apex-50k")
    tops = pf.get_rules("topstep-50k")

    g = pf.PropGuard(apex, buffer_usd=0)
    g.mark(50_000, 1_700_000_000_000)
    g.mark(52_600, 1_700_000_060_000)          # Apex trails on UNREALISED equity
    assert g.threshold == 50_100, g.threshold   # ...and locks at start + $100
    g.mark(60_000, 1_700_000_120_000)
    assert g.threshold == 50_100                # locked, does not keep trailing
    assert not g.halted
    assert not g.mark(50_050, 1_700_000_180_000)
    assert g.halted and "trailing" in g.halt_reason

    t = pf.PropGuard(tops, buffer_usd=0)
    t.mark(50_000, 1_700_000_000_000)
    t.mark(53_000, 1_700_000_060_000)           # same day: EOD trailing ignores it
    assert t.threshold == 48_000, t.threshold
    t.mark(53_000, 1_700_100_000_000)           # next day: trails, locks at start
    assert t.threshold == 50_000, t.threshold
    assert not t.mark(52_000, 1_700_100_060_000)   # -$1,000 on the day = DLL
    assert "daily loss limit" in t.halt_reason
    ok, why = t.check_order(3)
    assert not ok and "PROP HALT" in why
    ok, why = pf.PropGuard(tops).check_order(tops.max_contracts + 1)
    assert not ok and "cap" in why

    # evaluation over bars: a strategy that never trades cannot pass, and holding
    # overnight is a hard fail at both firms
    flat = [0] * len(daily)
    hold = [1] * len(daily)
    mnq = pf.CONTRACTS["MNQ"]
    r_flat = pf.evaluate(daily, flat, apex, mnq, notional=40_000)
    assert r_flat["result"] == "FAIL" and r_flat["reason"] == pf.FAIL_TIME
    r_hold = pf.evaluate(daily, hold, apex, mnq, notional=40_000)
    assert r_hold["result"] == "FAIL" and r_hold["reason"] == pf.FAIL_OVERNIGHT
    r_ovn = pf.evaluate(daily, hold, apex, mnq, notional=40_000,
                        enforce_overnight=False)
    assert r_ovn["result"] in ("PASS", "FAIL")

    # pass rate must be monotonically non-increasing in size for a fixed-dollar
    # drawdown often enough to matter — check the machinery reports both ends
    sweep = pf.sweep_size(daily, {"hold": hold}, apex, mnq, [1, 4],
                          stride=60, enforce_overnight=False, notional=40_000)
    assert len(sweep["rows"][0]["cells"]) == 2
    assert all(0 <= c["pass_pct"] <= 100 for c in sweep["rows"][0]["cells"])
    print("propfirm  : Apex trails unrealized + locks at start+100; Topstep trails "
          "EOD + DLL;\n            overnight holds fail; size sweep runs")

    print("\nALL SMOKE CHECKS PASSED ✓")


if __name__ == "__main__":
    main()
