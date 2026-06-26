# Crypto Signal Scout 🛰️

Pull **free, no-API-key** market data from Binance's public REST API, run real
technical-analysis signal scouting, and overlay an esoteric reference layer
(Fibonacci / golden ratio / biblical & Gann numbers) — all rendered into a
single self-contained `crypto_dashboard.html`.

## Run it

```bash
# Live data (needs outbound access to api.binance.com)
python3 crypto_signal_scout.py --symbols BTCUSDT ETHUSDT SOLUSDT --interval 1d --limit 365

# Offline demo (synthetic data, no network — for trying the dashboard)
python3 crypto_signal_scout.py --demo
```

Then open `crypto_dashboard.html` in a browser.

No dependencies beyond the Python 3 standard library. The script honors
`HTTPS_PROXY` automatically.

## What it computes

- **Standard TA (real):** RSI(14), SMA20/SMA50 crossovers (golden/death cross),
  10-period momentum, 20-period volatility, swing support/resistance.
- **Theory layer (curiosity):** scans where the latest price coincides (within a
  tolerance) with Fibonacci retracements, golden-ratio projections, and biblical
  numbers (7, 12, 40, 153, 666, 144000, …).

## ⚠️ Honesty / disclaimer

Two different kinds of "signal" live here:

1. The **TA block** is standard technical analysis. It is **not financial advice**
   and has no guaranteed predictive power.
2. The **theory layer** is included because it was requested. A price "lining up"
   with a sacred number is **coincidence (apophenia)**, not a trading edge — with
   enough candidate numbers you can always find a hit. **Do not risk money on it.**

Nothing in this repository is investment advice.

> Note: This was built in a sandbox whose network policy blocks Binance, so live
> fetching was validated by code review + the offline `--demo` path. Run it on a
> machine with Binance access to pull live candles.
