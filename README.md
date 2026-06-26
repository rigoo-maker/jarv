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
