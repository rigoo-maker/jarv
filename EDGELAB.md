# 🔬 edgelab — did we actually find an edge?

A harness for answering the one question `krypt`, `solai` and `topstep` could
all backtest around but none could answer: **is the apparent edge real, or an
artifact of having looked at many strategies until one looked good?**

Run on **real market data**: 32,110 daily split-adjusted bars, 10 US ETFs,
2014-01-02 → 2026-10-08, zero gap-fill bars.

```bash
python3 -m edgelab.report data/etf_daily.csv
```

## Result: no edge survived

70 strategy×symbol tests. Corrected t-threshold for 70 tests: **3.38**.
Exactly one test cleared it — and it was **negative in-sample (t = −3.81) and
positive out-of-sample (t = +1.13)**. A sign flip is the signature of noise,
not of an edge.

| signal | sym | trades/yr | IS Sharpe | deflated | IS t | OOS Sharpe | OOS t | OOS maxDD |
|---|---|---|---|---|---|---|---|---|
| reversal2d | XLK | 44 | 1.03 | 0.89 | 2.71 | 1.08 | 2.59 | 8.4% |
| buy&hold | QQQ | 252 | 1.00 | 0.94 | 2.63 | 0.79 | 1.89 | 35.6% |
| ovn+trend | QQQ | 252 | 0.95 | 0.89 | 2.36 | 1.08 | 2.58 | 15.8% |
| trend200 | QQQ | 252 | 0.94 | 0.87 | 2.33 | 1.03 | 2.47 | 21.3% |

`reversal2d XLK` looks like the find: Sharpe 1.03 → 1.08 out-of-sample with an
8.4% drawdown. It is not. Two tests dismantle it.

## Three mistakes this harness exists to catch

It caught all three in its own first run.

**1. Annualising per-trade returns as if they were daily.** The first version
of this scan reported `reversal2d XLK` at Sharpe **2.64**. It trades 44 times a
year; scaling 44 observations by √252 inflates Sharpe by √(252/44) = **2.4×**.
`perf()` derives annualisation from calendar span, never from bar count.

**2. Reporting the best of N tests without paying for N.** Seventy tests gives
a ~97% chance of at least one t > 2 on pure noise. The best in-sample t here
was 2.71 against a required 3.38.

**3. Judging on unlevered Sharpe.** A low-volatility, high-Sharpe strategy has
to be levered to compete, and financing that leverage is usually what kills it.

## The increment test — why "reversal" isn't timing

A signal can show a strongly positive mean and carry no information, because
the asset drifts up on its own. What makes a signal a signal is the
**increment** over simply being invested. Pooled across 8 ETFs as a single
hypothesis:

| signal | window | edge vs buy&hold | t |
|---|---|---|---|
| after 2 down closes | in-sample | +4.28 bp/trade | 1.25 |
| after 2 down closes | out-of-sample | +3.77 bp/trade | 1.23 |
| after 3 down closes | in-sample | +1.88 bp/trade | 0.38 |
| after 3 down closes | out-of-sample | +14.76 bp/trade | 3.27 |
| **overnight only** | in-sample | **−2.88 bp/trade** | **−2.10** |
| **overnight only** | out-of-sample | **−4.00 bp/trade** | **−2.83** |

The 2-down-close effect is **+3.8 bp with t ≈ 1.2** — the raw +7.9 bp/trade is
mostly the equity risk premium collected on a subset of days. The one
significant cell (3 down, OOS, t = 3.27) was t = 0.38 in-sample.

And the overnight anomaly is **significantly worse than holding** once you pay
the spread 252 times a year — one of the few statistically solid results here,
pointing the wrong way.

## The acid test — leverage and financing

Out-of-sample, levered to QQQ's volatility, financing charged at 5%:

| strategy | leverage | unlevered Sharpe | CAGR | maxDD | vs QQQ |
|---|---|---|---|---|---|
| QQQ buy&hold | 1.00 | 0.79 | 16.3% | 35.6% | — |
| **EW 8-ETF buy&hold** | 1.44 | **0.92** | **17.1%** | 27.6% | **+0.8 pp** |
| EW 8-ETF overnight | 2.42 | 0.45 | 0.5% | 53.1% | −15.8 pp |
| EW 8-ETF ovn+trend | 3.67 | 0.83 | 2.7% | 56.0% | −13.5 pp |

`ovn+trend` has a real 0.83 Sharpe and a 13.4% unlevered drawdown — genuinely
attractive looking. Levered 3.67× to compete, financing costs 13.4%/yr and it
returns **2.7% against the benchmark's 16.3%**. The Sharpe was real; the
tradeable edge was not.

## What did survive

Two things, neither of them clever:

1. **The equity risk premium.** +4.31 bp/day across 32,100 observations,
   **t = 6.05**. Overwhelming, and available by doing nothing.
2. **Diversification.** Equal-weighting 8 ETFs instead of holding QQQ raised
   out-of-sample Sharpe 0.79 → 0.92 and cut max drawdown 35.6% → 19.0%. It is
   the only thing in this study that beat the benchmark after leverage and
   financing, and it required no forecast.

## Limitations

- **One market, one regime.** 12.8 years of a mostly-rising US equity market.
  Nothing here is evidence about other assets or a secular bear.
- **Daily bars only.** Intraday microstructure effects are invisible at this
  resolution; the overnight/intraday split is the finest cut available.
- **Costs are modelled, not measured.** 1.0–2.2 bp round trip per ETF. Your
  real fills decide the outcome for anything high-turnover.
- **The grid is small.** 7 signals × 10 symbols. A negative result over 70
  tests is not proof that no edge exists — only that none of these is one.
- **Survivorship.** All 10 ETFs still exist and still trade. That bias is
  small for broad ETFs and real nonetheless.
