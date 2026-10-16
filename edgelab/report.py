"""python3 -m edgelab.report data/etf_daily.csv — the full honest write-up."""

from __future__ import annotations

import sys

from .scan import load_csv, scan, increment_test, acid_test
from .signals import SIGNALS, reversal, overnight_trend, buy_and_hold, overnight

SPLIT = "2021-01-01"
EQUITY_SLEEVE = ["SPY", "QQQ", "IWM", "XLK", "XLF", "XLE", "XLU", "XLV"]


def equal_weight(data, fn, symbols, **kw):
    from collections import defaultdict
    acc = defaultdict(list)
    for s in symbols:
        for d, r in fn(data[s], symbol=s, **kw):
            acc[d].append(r)
    return [(d, sum(acc[d]) / len(acc[d])) for d in sorted(acc)]


def main(path="data/etf_daily.csv"):
    data = load_csv(path)
    bars = sum(len(v) for v in data.values())
    span = (min(b["date"] for v in data.values() for b in v),
            max(b["date"] for v in data.values() for b in v))
    print(f"edgelab report — {len(data)} symbols, {bars:,} daily bars, "
          f"{span[0]} .. {span[1]}")
    print(f"discovery window < {SPLIT}   |   held-back window >= {SPLIT}\n")

    # 1. the grid -----------------------------------------------------------
    r = scan(data, SPLIT)
    print(f"[1] GRID SCAN — {r['n_tests']} tests. "
          f"Bonferroni t for {r['n_tests']} tests @p=0.05: {r['bonferroni_t']:.2f}")
    print(f"{'signal':12}{'sym':5}{'tr/yr':>7}|{'IS Shrp':>9}{'defl':>7}{'IS t':>7}"
          f"|{'OOS Shrp':>10}{'OOS t':>7}{'OOS DD%':>9}")
    print("-" * 80)
    for x in r["rows"][:10]:
        a, b = x["is"], x["oos"]
        print(f"{x['signal']:12}{x['symbol']:5}{a['obs_per_year']:>7.0f}"
              f"|{a['sharpe']:>9.2f}{a['deflated_sharpe']:>7.2f}{a['t_stat']:>7.2f}"
              f"|{b['sharpe']:>10.2f}{b['t_stat']:>7.2f}{b['max_dd_pct']:>9.1f}")
    print(f"\n  survivors clearing the corrected threshold: {len(r['survivors'])}")
    for s in r["survivors"]:
        print(f"    {s['signal']} {s['symbol']}: IS t={s['is']['t_stat']:+.2f} "
              f"-> OOS t={s['oos']['t_stat']:+.2f}  "
              f"{'SIGN FLIP — noise' if s['is']['t_stat']*s['oos']['t_stat'] < 0 else ''}")

    # 2. the increment ------------------------------------------------------
    print(f"\n[2] INCREMENT TEST — pooled across {len(EQUITY_SLEEVE)} ETFs, "
          f"one hypothesis each.\n    Does the signal beat simply being invested?")
    print(f"{'signal':16}{'window':16}{'edge bp/trade':>15}{'t':>8}{'n':>8}")
    print("-" * 63)
    for label, fn, kw in (("after 2 down", reversal, {"n_down": 2}),
                          ("after 3 down", reversal, {"n_down": 3}),
                          ("overnight only", overnight, {})):
        res = increment_test(data, fn, SPLIT, EQUITY_SLEEVE, **kw)
        for w in ("in_sample", "out_of_sample"):
            v = res[w]
            if v:
                print(f"{label:16}{w:16}{v['edge_bp']:>15.2f}{v['t_stat']:>8.2f}"
                      f"{v['n_sample']:>8}")

    # 3. the acid test ------------------------------------------------------
    print(f"\n[3] ACID TEST — levered to the benchmark's volatility, "
          f"financing charged at 5%")
    bench = {"name": "QQQ buy&hold",
             "series": buy_and_hold(data["QQQ"], symbol="QQQ")}
    cands = [
        {"name": "EW 8-ETF buy&hold",
         "series": equal_weight(data, buy_and_hold, EQUITY_SLEEVE)},
        {"name": "EW 8-ETF overnight",
         "series": equal_weight(data, overnight, EQUITY_SLEEVE)},
        {"name": "EW 8-ETF ovn+trend",
         "series": equal_weight(data, overnight_trend, EQUITY_SLEEVE)},
    ]
    at = acid_test(data, cands, bench, SPLIT)
    print(f"{'strategy':22}{'lev':>6}{'unlev Shrp':>12}{'CAGR%':>8}"
          f"{'maxDD%':>8}{'vs bench pp':>13}")
    print("-" * 69)
    for x in at["results"]:
        u = x.get("unlevered", x["perf"])
        print(f"{x['name']:22}{x['leverage']:>6.2f}{u['sharpe']:>12.2f}"
              f"{x['perf']['cagr_pct']:>8.1f}{x['perf']['max_dd_pct']:>8.1f}"
              f"{x['vs_benchmark_pp']:>+13.1f}")

    print("\n[4] VERDICT")
    print("    No signal in this grid survived correction for the size of the")
    print("    search. The reversal effect is the asset's own drift collected on")
    print("    fewer days, not timing: its increment over being invested runs")
    print("    t~1.2-1.5. The overnight+trend variant does have a genuinely low")
    print("    drawdown, and loses anyway once levered to a comparable")
    print("    volatility, because financing the leverage costs more than the")
    print("    edge delivers. The only effects that survive are the equity risk")
    print("    premium itself and diversification.")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
