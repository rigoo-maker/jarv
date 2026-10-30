# 📈 TopStep / ProjectX futures strategy toolkit

Strategies, an evaluation-aware backtester, and a pre-trade risk governor for
TopStep's Trading Combine via the TopstepX (ProjectX Gateway) API.

> **Not financial advice.** Futures are leveraged. This is a research harness.

---

## The premise

On a funded-account evaluation, **the trailing drawdown decides whether you
pass, not the entry signal.** So the rules engine is the core of this package
and the strategies are interchangeable inputs to it.

The $50K Combine asks you to make **$3,000 while never giving back $2,000 from
your peak** — a 1.5:1 target-to-drawdown ratio. Three things follow that most
people get wrong:

**1. The floor ratchets up and never comes down.** Win $2,500, give back
$2,600, and you are out at *net −$100*. The smoke test asserts this exact case
because it is the most common way a profitable trader fails.

**2. On day one the *daily* limit binds, not the trailing one.** Your real
stop-out is $1,000 away, not $2,000. Sizing off the MLL doubles your risk.

**3. A zero-edge trader passes about 26% of the time, not 40%.** The naive
random-walk answer — `2000/(3000+2000)` = 40% — is wrong because the barrier is
*trailing*, not fixed: once you touch +$2,000 the floor locks at breakeven and
your remaining downside collapses from $2,000 to $0. Simulated, it is ~25.6%
with unlimited time and far lower with a day limit.

## The finding that matters most

Same edge (50% win, 2:1 R:R) — **only the position size changes**:

| risk/trade | expectancy | PASS % | trailing MLL | daily limit | timeout | binding constraint |
|---|---|---|---|---|---|---|
| $25 | +$11 | 2.9% | 0% | 0% | 97% | can't get there in time |
| $50 | +$24 | 92.0% | 0% | 0% | 8% | — |
| **$100** | +$49 | **99.8%** | 0% | 0% | 0% | — |
| **$150** | +$74 | **99.6%** | 0.4% | 0% | 0% | — |
| $200 | +$99 | 97.7% | 2.3% | 0% | 0% | — |
| $300 | +$149 | 52.7% | 7.7% | 0% | 0% | trailing MLL |
| $400 | +$199 | 32.8% | 0% | 41% | 0% | daily loss limit |
| $500 | +$249 | 20.7% | 0% | 61% | 0% | daily loss limit |

Expectancy per trade rises monotonically with size. Pass rate does not — it
peaks around $100–150 and then collapses. **Maximising expectancy and
maximising your chance of passing are different objectives**, and the
evaluation pays only for the second. Find your own band:

```bash
python3 -m topstep.app sweep --win 0.55 --rr 1.5
```

## Quick start

```bash
python3 -m topstep._smoke                       # 38 checks, no network, no key
python3 -m topstep.app rules  --tier 50K        # the limits you actually trade under
python3 -m topstep.app size   --tier 50K        # contracts per stop distance
python3 -m topstep.app sweep  --win 0.55 --rr 2 # find the viable risk band
python3 -m topstep.app backtest --tape trend    # strategies vs the account rules
python3 -m topstep.app doctor                   # gateway reachability
```

Pure stdlib. Indicators are reused from `krypt/`.

## What's in it

| Module | Role |
|---|---|
| `rules.py` | **The core.** Trailing MLL (intraday *and* end-of-day modes), daily loss limit, consistency rule, breach state machine, risk-based sizing. |
| `live.py` | Pre-trade governor. Every order passes `approve()`; reject-by-default. Paper broker; live broker is a locked stub. |
| `backtest.py` | Walks bars under full account rules → PASS / FAIL / BREACH. Plus `monte_carlo()` over the whole evaluation. |
| `strategies.py` | Five intraday strategies — ORB, VWAP reversion, trend pullback, range fade, momentum continuation. |
| `instruments.py` | CME contract specs (tick size/value, commissions) for ES/NQ/RTY/YM/CL/GC + micros. |
| `api.py` | TopstepX REST client. All endpoints and enums in one `WIRE` block. |

## Sizing is derived from the stop, never from equity

```python
qty = state.max_contracts_for_risk(instrument, stop_ticks, risk_fraction=0.25)
```

`risk_fraction` is a share of **remaining room**, not of account equity. Sizing
off equity is the classic error: 1% of $50,000 is $500 — a quarter of the
account's entire life on one trade. A signal with no stop is rejected outright,
because without a stop there is no size.

## Live trading

`LiveBroker` raises `NotImplementedError` behind two locks (`TOPSTEP_MODE=live`
and `TOPSTEP_ALLOW_LIVE=1`). This is the design, not an unfinished corner.
`live.py` documents the five things a real implementation must handle — linked
protective stops, position reconciliation against the gateway, partial fills,
rejects/disconnects, and believing the platform's drawdown over your own model.

## Known limitations — read these

1. **The API is unverified against a live gateway.** This sandbox's egress
   policy blocks `api.topstepx.com`, so paths and enums follow the published
   ProjectX spec but are untested on the wire. They are isolated in the `WIRE`
   block in `api.py` — run `doctor`, fix that one block if anything 404s.
2. **Account specs change.** Targets, drawdowns and the consistency rule are
   from published Combine tiers; verify against your account before sizing.
3. **`trail_mode` is the single most consequential setting here.** Whether the
   MLL trails *intraday equity* or *end-of-day balance* changes safe position
   size by roughly 2x. Default is `eod`; both are implemented. **Confirm which
   your account uses.**
4. **The five strategies are hypotheses, not edges.** They lose money on the
   synthetic fixtures, which is the correct result — random-walk data has no
   edge and the cost model bites. They tell you nothing until run on real bars.
5. **No order-book or tick data.** Bar-close fills with fixed slippage. Real
   intraday fills are worse than this models, especially on stops.
6. **Commissions are defaults, not your rates.** They dominate at micro size:
   in testing, a 60%-win-rate strategy still lost money because 100 trades cost
   $3,637 in commissions.

## Honest expectations

The realistic output of this is **a decision about whether to fund an
evaluation at all** — and for most strategies the honest answer is no. That
answer is cheap here and expensive at $165 a reset.

Run `sweep` before you run anything else. If no risk size gives you a pass rate
you'd bet on, the strategy is the problem, and no amount of position-size
tuning fixes a missing edge.
