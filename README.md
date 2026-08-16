# ⚡ KRYPT Trader

An advanced crypto trading toolkit for Binance: live market data, an advanced
technical-analysis engine, a weighted signal-scoring model, HFT-style scalping,
delta hedging, a live auto-refreshing dashboard, and **hard safety guardrails**
around real-money trading.

> **This is not financial advice. Trading crypto can lose all your capital.**
> Read the safety section before you ever touch live mode.

---

## Safety model (read this first)

Live trading places **real orders with your money**. KRYPT is built so you
cannot do that by accident:

| Guardrail | Default | Meaning |
|-----------|---------|---------|
| **Mode** | `analyze` | Read-only. No orders. `paper` simulates; `live` is real. |
| **Two-lock live** | locked | Live needs `--mode live` **AND** env `KRYPT_ALLOW_LIVE=1`. |
| **Testnet** | `on` | Even in live mode, orders hit Binance **testnet** (fake money) until you set `BINANCE_TESTNET=0`. |
| **Max order / position** | $50 / $200 | Orders above the cap are rejected pre-trade. |
| **Max daily loss** | $100 | Cumulative realized loss halts trading. |
| **Kill switch** | 4 losses | N consecutive losers in a row halts trading. |
| **Keys** | env only | Never in code or git. `.env` is gitignored. |
| **Audit log** | on | Every intent/fill written to `krypt_audit.jsonl`. |

All limits are tunable via env vars (see `.env.example`).

## Setup

```bash
cp .env.example .env        # then edit .env with your keys + limits
# For first live tests, use TESTNET keys from https://testnet.binance.vision
set -a; source .env; set +a
```

Core (data, indicators, scoring, backtest, paper, dashboards) is **pure stdlib**.
Live trading on **Coinbase** additionally needs `pip install PyJWT cryptography`.

## Execution venues (`KRYPT_VENUE`)

KRYPT separates **data** (always free/public) from **execution** (where orders go).

| Venue | Auth | Testnet (fake money) | Deps |
|-------|------|----------------------|------|
| `binance` | HMAC-SHA256 | ✅ yes (default-on) | none |
| `coinbase` | JWT / ES256 (CDP key) | ❌ **none** — use `KRYPT_MODE=paper` | PyJWT, cryptography |

```bash
# Route live orders to Coinbase Advanced Trade
export KRYPT_VENUE=coinbase
export COINBASE_API_KEY_NAME='organizations/<org>/apiKeys/<uuid>'
export COINBASE_API_PRIVATE_KEY='-----BEGIN EC PRIVATE KEY-----\n...\n-----END EC PRIVATE KEY-----'
python3 -m krypt.app serve --mode paper      # SIMULATE first (Coinbase has no testnet)
```

> ⚠️ **Coinbase has no fake-money testnet** for this path, so `live` mode there is
> **real money immediately**. Prove your strategy in `--mode paper` (simulated fills
> against live prices) before ever switching to `live`. Keys come from env only —
> never hardcoded, never committed, never pasted into chat.

## Usage

```bash
# Read-only snapshot -> writes krypt_dashboard.html
python3 -m krypt.app analyze --symbols BTCUSDT --interval 1m

# Live auto-refreshing dashboard + trading loop at http://localhost:8787
python3 -m krypt.app serve --strategy scalper --mode paper

# Backtest the scoring engine on historical candles
python3 -m krypt.app backtest --symbols BTCUSDT --interval 1h --limit 500

# Which strategies still have an edge? -> krypt_heatmaps.html
python3 -m krypt.app heatmap --days 30 --interval 1m

# Export the survivors as NinjaTrader 8 strategies -> ./ninja/*.cs
python3 -m krypt.app ninja --days 30 --top 3

# Offline self-test (no network)
python3 -m krypt._smoke
```

To go live (deliberately):

```bash
export KRYPT_MODE=live KRYPT_ALLOW_LIVE=1 BINANCE_TESTNET=1   # testnet first!
python3 -m krypt.app serve --strategy scalper
# Only after testnet looks right: BINANCE_TESTNET=0  (REAL money)
```

## Finding an edge: 10 strategies, ranked honestly

KRYPT ships **10 distinct strategies** (`krypt/strats.py`) — `ema_cross`,
`macd_cross`, `rsi_reversion`, `bb_breakout`, `bb_reversion`, `donchian_breakout`,
`vwap_reversion`, `stochrsi_cross`, `adx_di`, `confluence`. The workflow:

```bash
python3 -m krypt.app download --days 21                      # 3 weeks of 1m candles
python3 -m krypt.app compare  --days 21 --interval 1m        # rank all 10 after costs
python3 -m krypt.app serve    --mode paper --strategy <winner>  # live-paper the best
```

`compare` runs every strategy through the realistic backtester (fees + slippage +
compounding) and prints a leaderboard ranked by Sharpe. **The top row is a
hypothesis, not an edge** — it's in-sample. Re-run on a *different* date range
(out-of-sample) before believing it. That discipline is the actual edge.

## Which edge is *still* active: the heatmaps

`compare` answers "what won over the whole sample?", which is the wrong question:
a strategy can post a great total and be **dead for the last week**. `heatmap`
slices the sample instead of averaging it away.

```bash
python3 -m krypt.app heatmap --days 30 --interval 1m --windows 12
# -> ranked ACTIVE EDGE table in the terminal + krypt_heatmaps.html
```

Seven maps, each answering one question (open the HTML — hover any cell for its
full stats, "Show numbers" prints them in the grid, and it works offline):

| Map | Question it answers |
|-----|---------------------|
| **Edge over time** (strategy × time slice) | Is the edge alive, or is it a memory? Blue left / red right = decayed. |
| **Regime map** (strategy × trend strength × volatility) | *Where* the money came from. Mean reversion should pay in **chop** and bleed in **strong** trends; breakout the reverse. Equal everywhere = probably just long the drift. |
| **Cost sensitivity** (strategy × fee+slippage) | Edge, or fee subsidy? A row that's blue at 0 bps and red by 10 never had an edge. |
| **Session map** (strategy × hour UTC) | Time-of-day effects — treat with suspicion, 24 columns is a lot of chances to find noise. |
| **Correlation of net returns** | Two strategies above ~0.8 are one trade with two names: stacking them doubles risk, not edge. |
| **Position overlap** | Cosine similarity of the raw positions — correlation says results agree, this says the positions do. |
| **Parameter sweeps** (RSI thresholds, VWAP band) | A real edge is a **plateau**; one hot cell in a cold field is a curve fit. |

### The ACTIVE EDGE score

Every map folds into one ranking that deliberately penalizes strategies whose
edge is in the past:

```
score = 0.30 recency-weighted window Sharpe   (half-life ¼ of the sample)
      + 0.25 OUT-OF-SAMPLE Sharpe             (last 30% of bars, held out)
      + 0.20 hit rate across windows
      + 0.15 improving-vs-decaying
      + 0.10 full-sample Sharpe
      × reliability (too few trades to trust) × consistency (0.6 + 0.4 × hit rate)
      capped at 45 if the held-out slice lost money
```

Verdicts are `ACTIVE EDGE` / `WEAK / WATCH` / `FADING` / `NO EDGE` / `THIN SAMPLE`.
All Sharpes are **annualized and net of costs** (note: `backtest`'s own Sharpe uses
a different, per-sample convention — the two are not comparable).

**It is a ranking heuristic, not proof.** Ten strategies across a dozen windows,
nine regimes and dozens of parameter cells is hundreds of comparisons — some cells
are blue by luck. The out-of-sample column and the plateau test fight that; they
don't win it. Re-run on another date range and another symbol before believing a row.

## Other markets: CSV / Kaggle data and the equity strategy library

The crypto rules do not transfer to daily equities, so there is a second library
(`krypt/strats_equity.py`) written for them, and a loader that eats arbitrary
OHLCV CSVs (Kaggle dumps, broker exports, TradingView):

```bash
python3 -m krypt.app heatmap --source csv --file nasdq.csv --symbols NDAQ \
        --strats equity --fee-bps 1 --slippage-bps 2 --out krypt_nasdaq.html

python3 -m krypt.app heatmap --source kaggle --dataset sai14karthik/nasdq-dataset \
        --strats equity          # needs `pip install kagglehub` + Kaggle creds
```

The loader sniffs delimiters, column aliases and date formats (including telling
`DD/MM` from `MM/DD` by testing the whole column, not the first row), filters
multi-symbol files, and hands extra columns (VIX, rates, gold, oil) to the
strategies that can use them.

**The equity library** — `sma200_trend`, `golden_cross`, `connors_rsi2`,
`ibs_reversion`, `gap_fade`, `turn_of_month`, `momentum_12_1`, `high52_breakout`,
`vix_calm`, `vix_spike_reversal` — is long/flat by design (no shorting single
names), gates most rules on the 200-day line, uses RSI(2) rather than RSI(14) for
reversion, and reads VIX directly when the dataset carries it.

Three things change automatically when the bars are not crypto minutes:

- **Annualization is measured from the timestamps**, not assumed. A 24/7 constant
  counts 365 bars a year on daily equities instead of ~252 and inflates every
  Sharpe by ~20%; on intraday equity bars the error is nearly 2x.
- **Buy & hold is added as a benchmark row** on the same bars and the same costs.
  A rule that does not beat it risk-adjusted is labelled **BETA ONLY** — it is the
  market with extra commissions. This matters more than any other column: most
  long-biased "edges" in a rising market are beta.
- **The regime axis switches** from ADX buckets to the 200-day line (below /
  above-flat / above-rising), and an **overnight-vs-intraday decomposition** is
  added — equities pay unevenly across the session, crypto has no session.

### A worked example, including the answer being "no"

Run on NDAQ daily bars (2010-2024, 3,914 candles, 1 bps fee + 2 bps slippage):

| | return | Sharpe | max DD | exposure |
|---|---|---|---|---|
| **buy & hold** | **+700%** | **0.73** | 38.6% | 100% |
| momentum_12_1 | +367% | 0.61 | 46.5% | 82% |
| golden_cross | +349% | 0.63 | 38.6% | 77% |
| sma200_trend | +238% | 0.56 | 48.1% | 77% |
| connors_rsi2 | +45% | 0.37 | 19.2% | 11% |

Not one rule beat buy & hold on return *or* Sharpe. The cost map explains half of
it — every reversion rule dies between 5 and 20 bps per side, while the slow trend
rules are cost-insensitive but simply lag the index. That is a real result, and
the report says it in the headline rather than crowning the least-bad row.

Read it with the sample in mind: one stock, in a 15-year uptrend, with no 2008 in
the window — precisely the conditions where a 200-day filter cannot win. The same
maps on a bear-inclusive range are the interesting follow-up.

## Finding strategies: search → sweep → hybrid → proof

`discover` runs the whole pipeline in one pass, ordered so no stage can cheat the
next:

```bash
python3 -m krypt.app discover --source csv --file nasdq.csv --symbols NDAQ \
        --strats equity --fee-bps 1 --slippage-bps 2
```

1. **Sweep** every rule's parameter grid (226 cells across 10 rules for the equity
   library) and pick each rule's best cell **in-sample only**. Choosing a
   parameter by its out-of-sample result and then reporting that result as
   out-of-sample is the oldest way to fool yourself in this business.
2. **Hybridise** the tuned rules five ways — `AND` (both long), `OR` (either),
   `k-of-n` vote, `SWITCH` (one rule above the 200-day line, another below), and
   equal-weight `portfolio` (splits capital, rebalances daily, combines equity
   curves rather than signals).
3. **Prove or discard**, with three tests most strategies fail:
   - **next-open fills** — refill every trade at the next bar's open instead of
     the close that produced the signal. A rule that needs the close it just saw
     is not tradeable.
   - **timing permutation** — slide the exact position series (same trades, same
     holding periods, same exposure) to random points in history a few thousand
     times. If the real Sharpe sits inside that distribution, the timing carried
     no information.
   - **multiple testing** — Benjamini-Hochberg across every hypothesis the run
     explored, after collapsing near-duplicates, because eleven combinations
     containing the same rule are eleven copies of one test and feeding copies to
     BH manufactures significance.

### What it found on NDAQ (14 years, 3,914 daily bars)

| candidate | Sharpe | next-open | exposure | p | p adj. | p (held-out) |
|---|---|---|---|---|---|---|
| connors_rsi2* SWITCH turn_of_month* | 1.14 | 0.90 | 14% | 0.0006 | 0.063 | 0.013 |
| connors_rsi2* (RSI2<5, SMA250) | 1.12 | 0.94 | 6% | 0.0006 | 0.063 | 0.007 |
| **buy & hold** | **0.73** | 0.74 | 100% | — | — | — |
| connors_rsi2 (untuned) | 0.37 | 0.38 | 11% | 0.24 | 1.0 | 0.08 |
| sma200_trend | 0.56 | 0.57 | 77% | 0.72 | 1.0 | 0.16 |

Findings, in order of how much they should change what you do:

- **Every trend rule fails the permutation test.** `sma200_trend` scores p = 0.72
  — its own position pattern applied at *random dates* does better more than
  two-thirds of the time. Its return comes from being in the market 77% of the
  time, not from choosing when. Same story for `golden_cross` and
  `momentum_12_1`. They are beta.
- **One signal type stands out: RSI(2) mean reversion inside a long trend
  filter.** It is the only family with p below 0.001, and the only one whose
  timing is still significant on the held-out tail alone (p = 0.007). At 6%
  exposure it earns Sharpe 1.12 against buy & hold's 0.73 — better paid per unit
  of risk, far worse in absolute return (139% vs 700%).
- **The hybrids mostly change exposure, not edge.** Eleven combinations "survived"
  before de-duplication and every one contained the same RSI(2) rule: one finding
  wearing eleven names. Only `SWITCH turn_of_month` adds anything, and it adds
  exposure rather than signal.
- **Nothing clears a correction for 400 hypotheses.** The best candidate lands at
  p_adj 0.063 against a 0.05 bar. That is a *candidate*, not an edge — and the way
  it becomes one is a different symbol or date range, where it is a single
  hypothesis instead of one of hundreds and the same p-value would be conclusive.
- **The untuned rule does not survive** — only the parameter-tuned variant does,
  which is exactly what the correction exists to catch.

So: **focus on short-horizon mean reversion, not trend following**, and prove it
somewhere else before sizing it.

## Prop-firm rules (Apex, Topstep)

A funded-account evaluation is not a small trading account — it is a different
game, and a strategy with a genuine edge can be structurally unable to pass it:

| Rule | Why it kills strategies |
|------|-------------------------|
| **Trailing drawdown** | The kill line follows equity **up** and never comes back down. Run +$4,000 then give back $2,600 and an Apex 50k is dead — while showing +$1,400. |
| **Trails on *unrealized* equity** (Apex) | Your open profit raises the line. Letting a winner run, then giving it back, is the classic blow-up. |
| **Daily loss limit** (Topstep) | One bad session ends it, whatever the equity curve looks like. |
| **No overnight positions** | Everything must be flat before the close. Every swing strategy is disqualified before Sharpe is even discussed. |
| **Consistency** | One monster day can void a payout even when the total is fine. |

```bash
# would these strategies pass an evaluation, and at what size?
python3 -m krypt.app prop --source csv --file nasdq.csv --strats equity \
        --firm topstep-50k --contract MNQ --qty-sweep 1,2,3,4

# add the pass-rate map to the HTML report
python3 -m krypt.app heatmap --source csv --file nasdq.csv --strats equity \
        --prop --firm apex-50k

# emit NinjaScript with those rules compiled in
python3 -m krypt.app ninja --strategy connors_rsi2 --strats equity \
        --firm topstep-50k --prop
```

Presets: `apex-25k/50k/75k/100k/150k/250k/300k`, `topstep-50k/100k/150k` — or pass
a JSON file to `--firm` to override any field. **Rule numbers change**; every
preset carries an `as_of` marker and prints it with the results. Verify against
the firm's current rulebook before trusting a PASS.

### Evaluated from many start dates, not one

`prop` doesn't run one evaluation — it starts one every `--stride` bars and
reports the **distribution**. Passing once from a lucky start is an anecdote; a
strategy that passes 20% of the time is a strategy that fails 80% of the time,
and both numbers describe the same strategy.

### What it found on NDAQ dailies

Every strategy failed instantly on `held a position overnight` — which is the
correct answer, not a bug: **Apex and Topstep are intraday games**, and daily
swing rules cannot play. With that rule disabled to see the P&L side
(`--allow-overnight`, a counterfactual only), Topstep 50k on MNQ:

| strategy | 1 contract | 2 | 3 | 4 |
|---|---|---|---|---|
| connors_rsi2 | **20%** | 4% | 2% | 0% |
| turn_of_month | 13% | 4% | 0% | 0% |
| vix_spike_reversal | 12% | 4% | 0% | 0% |
| sma200_trend | 0% | 0% | 0% | 0% |

Size is the lever, and it is brutal. The drawdown is a **fixed number of dollars**
while your swings scale with contracts, so pass probability collapses long before
expectancy does. The best size is the smallest one that can still reach the target
in time — almost never the maximum the firm allows.

### Live guardrails, not just backtests

`propfirm.PropGuard` enforces the same rules on a running account and plugs into
`RiskEngine`: it tracks the trailing threshold against **unrealized** equity, halts
a configurable buffer *before* the line (being flat one tick early beats one tick
late), enforces the daily loss limit against the session's opening equity, and
caps contracts. `risk.snapshot()["prop"]` exposes threshold, room and day P&L.

Generated NinjaScript carries the same logic — `PropGuardOk()` runs before any
signal, flattens on breach, and flattens at `FlattenTime` so nothing goes
overnight. With `--prop` the account's numbers are compiled into the file's
defaults.

**Caveat that matters:** bar-close data approximates intrabar breaches from each
bar's high/low, and the firms enforce tick by tick. Treat every pass rate here as
an optimistic ceiling.

## Exporting to NinjaTrader (NinjaScript)

The strategies that survive the maps can be emitted as NinjaTrader 8 C# strategies:

```bash
python3 -m krypt.app ninja --days 30 --top 3          # export the ranked survivors
python3 -m krypt.app ninja --days 30 --strategy rsi_reversion   # export one by name
python3 -m krypt.app ninja --no-analysis              # bare templates, no measurements
```

Writes `ninja/Krypt*.cs` plus an install README. Every one of the 10 strategies has
a template (`ema_cross`, `macd_cross`, `rsi_reversion`, `bb_breakout`, `bb_reversion`,
`donchian_breakout`, `vwap_reversion`, `stochrsi_cross`, `adx_di`, `confluence` —
the last is a full port of the weighted scoring engine). Each generated file:

- **mirrors `strats.py` rule-for-rule** on bar close, converting where NinjaTrader
  differs (its StochRSI is 0..1, KRYPT's is 0..100; Donchian uses the *prior* N bars),
- carries **the measured evidence in its header** — edge score, verdict, OOS Sharpe,
  which regime the money came from, and the cost level where it stopped working,
- ships a **regime filter chosen by the regime map, not by the textbook**. If the
  data disagrees with "reversion likes chop", the file says so and ships the filter
  **off**,
- exposes stop/target/quantity/session/long-only as NinjaScript parameters,
- is pure ASCII (NinjaScript editors are not reliably UTF-8).

Install: copy the `.cs` files to `Documents\NinjaTrader 8\bin\Custom\Strategies\`,
press **F5** in the NinjaScript editor, then **backtest in Strategy Analyzer on your
instrument and your costs** — the KRYPT numbers came from crypto spot bars, a
different market with different microstructure. Sim101 before anything live.

### About leverage (read before you set `--leverage`)

Leverage is a knob, and the backtester models **liquidation** so you can see what
it really does. Example from a single 3-week sample (winners at 1×, then 10×):

| strategy | 1× return | 10× return |
|----------|-----------|------------|
| ema_cross | **+115%** | **−83%** |
| confluence | +32% | −49% |
| rsi_reversion | −51% | **−100% (liquidated)** |

Same strategy, same data — **10× turned winners into losers and liquidated
several to zero**, with max drawdowns near 100%. Leverage does not create edge;
it multiplies whatever edge (or lack of it) you have, including the path to ruin.
Start at `1×`. Raise it only on a strategy with a *proven, out-of-sample* edge,
and even then conservatively.

## What's inside

- **`indicators.py`** — advanced TA: EMA/SMA, RSI, MACD, Bollinger Bands, ATR,
  VWAP, Stochastic RSI, ADX/DI, OBV.
- **`scoring.py`** — confluence engine: weighted blend of all indicators into a
  single `[-100,+100]` bull/bear score with a per-indicator breakdown.
- **`strategies/`**
  - `scalper` — **HFT-style**: order-book imbalance + micro-momentum, tight
    spread filter, fast in/out.
  - `market_maker` — inventory-skewed spread capture.
  - `hedge` — **delta hedge**: offsets spot exposure with a futures short,
    sized dynamically by trend regime (ADX).
  - `trend` — slower trend-following on the scoring engine.
- **`analytics.py`** — the edge maps: window/regime/hour/cost matrices, return
  correlation + position overlap, parameter sweeps, and the ACTIVE EDGE score
  with an out-of-sample split.
- **`heatmap.py`** — self-contained HTML report (no CDN) with a computed
  diverging color scale — both arms generated from the same OKLab lightness
  steps, so a loss is exactly as loud as an equal gain, in light and dark.
- **`ninjascript.py`** — NinjaTrader 8 exporter: all 10 strategies as C#, with
  the measured evidence and a data-derived regime filter in each header.
- **`hybrids.py`** — AND / OR / k-of-n vote / regime-switch / equal-weight
  portfolio combinations, all judged on the same terms as a single rule.
- **`validate.py`** — the proof layer: next-open fills, timing-permutation
  p-values, Benjamini-Hochberg over everything explored, near-duplicate collapse.
- **`propfirm.py`** — Apex/Topstep rulebooks as an engine: trailing drawdown
  (unrealized vs end-of-day), daily loss limits, consistency, contract caps,
  evaluation across many start dates, and a live `PropGuard`.
- **`strats_equity.py`** — 10 US-equity daily rules (200-day gating, RSI(2)
  reversion, calendar and VIX effects), long/flat by design.
- **`csvdata.py`** — OHLCV loader for Kaggle dumps and broker exports.
- **`risk.py`** — position sizing, limits, daily-loss + kill-switch halts.
- **`trader.py`** — analyze/paper/live execution with audit logging.
- **`binance_client.py`** — stdlib REST client (public data + signed trading,
  spot + futures, testnet), with lot-size rounding.
- **`dashboard.py`** — live candlestick chart + score panel + order book + risk
  + alerts, auto-refreshing.

## Honesty about "HFT"

True microsecond HFT is **not** achievable from a retail Python REST client
(latency, colocation, rate limits). The scalper is *high-frequency-style*:
reacting to order-book pressure on the fastest candles. The edge is small, fees
and slippage are real, and the backtest is a toy (no fees/slippage modelled).
Prove everything on testnet/paper before risking a cent.

> Built in a sandbox whose network blocks Binance, so the trading paths were
> validated by an offline smoke test (`krypt._smoke`) + code review. Run it where
> Binance is reachable for live data.
