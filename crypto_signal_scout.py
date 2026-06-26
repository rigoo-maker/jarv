#!/usr/bin/env python3
"""
crypto_signal_scout.py
======================

Pull FREE (no-API-key) market data from Binance's public REST API, run real
technical-analysis signal scouting, AND overlay an "esoteric" reference layer
(Fibonacci / golden ratio / biblical & Gann numbers) so you can see where price
coincides with those numbers.

Output: a single self-contained `crypto_dashboard.html` you can open in a browser.

------------------------------------------------------------------------------
HONESTY NOTE (read this)
------------------------------------------------------------------------------
There are two completely different kinds of "signal" in here:

  1. STANDARD TA  (RSI, moving-average crossovers, momentum, volatility,
     support/resistance). These are widely used. They are NOT financial advice
     and have no guaranteed predictive power.

  2. THE "THEORY" LAYER  (Fibonacci retracements, golden ratio, biblical
     numbers like 7/12/40/144000/666, Gann levels). This is included because
     it was explicitly requested. It is presented as a CURIOSITY. Finding a
     price that "lines up" with one of these numbers is numerology / apophenia:
     with enough candidate numbers and tolerance you will ALWAYS find a hit by
     chance. It is not a trading edge. Do not risk money on it.

Nothing in this file is investment advice.
------------------------------------------------------------------------------

Usage:
    python3 crypto_signal_scout.py                      # defaults (BTC/ETH/SOL, 1d)
    python3 crypto_signal_scout.py --symbols BTCUSDT ETHUSDT --interval 4h --limit 500
    python3 crypto_signal_scout.py --demo               # offline synthetic data (no network)

The script honors HTTPS_PROXY from the environment automatically (urllib).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# Binance public hosts, tried in order. None require an API key for klines.
BINANCE_HOSTS = [
    "https://api.binance.com",
    "https://data-api.binance.vision",
    "https://api1.binance.com",
    "https://api.binance.us",
]

# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, interval: str, limit: int):
    """Fetch OHLCV candles from the first reachable Binance host.

    Returns a list of dicts: {time, open, high, low, close, volume}.
    Raises RuntimeError if every host is blocked/unreachable.
    """
    path = f"/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    last_err = None
    for host in BINANCE_HOSTS:
        url = host + path
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "signal-scout/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = json.loads(resp.read().decode())
            return [
                {
                    "time": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                }
                for c in raw
            ]
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError) as e:
            last_err = f"{host}: {e}"
            continue
    raise RuntimeError(
        "Could not reach any Binance host (all blocked or down).\n"
        f"  last error: {last_err}\n"
        "  If you're inside a locked-down sandbox, run with --demo, or run this\n"
        "  script somewhere with outbound access to api.binance.com."
    )


def synthetic_klines(symbol: str, n: int = 365, seed_price: float = 60000.0):
    """Deterministic offline data so the dashboard renders with no network.

    Pseudo-random walk (LCG, no imports) — clearly NOT real market data.
    """
    candles = []
    price = seed_price
    state = sum(ord(c) for c in symbol) * 2654435761 & 0xFFFFFFFF
    day_ms = 86_400_000
    base_t = 1_700_000_000_000
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        r = (state / 0x7FFFFFFF) - 0.5            # -0.5..0.5
        drift = 0.0008
        price *= (1 + drift + 0.03 * r)
        o = price
        c = price * (1 + 0.02 * r)
        hi = max(o, c) * (1 + 0.01 * abs(r))
        lo = min(o, c) * (1 - 0.01 * abs(r))
        candles.append({
            "time": base_t + i * day_ms,
            "open": o, "high": hi, "low": lo, "close": c,
            "volume": 1000 + 5000 * abs(r),
        })
        price = c
    return candles


# ---------------------------------------------------------------------------
# Standard technical-analysis signals (the real ones)
# ---------------------------------------------------------------------------

def sma(values, period):
    out = [None] * len(values)
    if len(values) < period:
        return out
    s = sum(values[:period])
    out[period - 1] = s / period
    for i in range(period, len(values)):
        s += values[i] - values[i - period]
        out[i] = s / period
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100 - 100 / (1 + (avg_gain / avg_loss if avg_loss else 1e9))
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gain = max(ch, 0)
        loss = max(-ch, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = avg_gain / avg_loss if avg_loss else 1e9
        out[i] = 100 - 100 / (1 + rs)
    return out


def volatility(closes, period=20):
    """Annualization-free stdev of returns over the last `period` candles (%)."""
    if len(closes) < period + 1:
        return None
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - period, len(closes))]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * 100


def ta_signals(candles):
    closes = [c["close"] for c in candles]
    last = closes[-1]
    s20, s50 = sma(closes, 20), sma(closes, 50)
    r = rsi(closes, 14)
    vol = volatility(closes, 20)

    findings = []

    # MA crossover
    if s20[-1] and s50[-1] and s20[-2] and s50[-2]:
        if s20[-2] <= s50[-2] and s20[-1] > s50[-1]:
            findings.append(("MA crossover", "BULLISH", "SMA20 just crossed ABOVE SMA50 (golden cross)"))
        elif s20[-2] >= s50[-2] and s20[-1] < s50[-1]:
            findings.append(("MA crossover", "BEARISH", "SMA20 just crossed BELOW SMA50 (death cross)"))
        else:
            trend = "above" if s20[-1] > s50[-1] else "below"
            findings.append(("MA trend", "NEUTRAL", f"SMA20 is {trend} SMA50"))

    # RSI
    if r[-1] is not None:
        if r[-1] >= 70:
            findings.append(("RSI(14)", "OVERBOUGHT", f"RSI = {r[-1]:.1f} (>=70)"))
        elif r[-1] <= 30:
            findings.append(("RSI(14)", "OVERSOLD", f"RSI = {r[-1]:.1f} (<=30)"))
        else:
            findings.append(("RSI(14)", "NEUTRAL", f"RSI = {r[-1]:.1f}"))

    # Momentum (10-period rate of change)
    if len(closes) > 10:
        roc = (last / closes[-11] - 1) * 100
        d = "BULLISH" if roc > 0 else "BEARISH"
        findings.append(("Momentum(10)", d, f"{roc:+.2f}% over last 10 candles"))

    if vol is not None:
        findings.append(("Volatility(20)", "INFO", f"{vol:.2f}% per-candle stdev"))

    return {
        "last": last,
        "sma20": s20[-1],
        "sma50": s50[-1],
        "rsi": r[-1],
        "volatility": vol,
        "findings": findings,
    }


# ---------------------------------------------------------------------------
# The "theory" layer (curiosity only — see HONESTY NOTE)
# ---------------------------------------------------------------------------

PHI = (1 + 5 ** 0.5) / 2  # golden ratio ~1.618

# Numbers drawn from the requested "Bible & other theories" themes.
BIBLICAL_NUMBERS = {
    3: "trinity / resurrection day",
    7: "completion (7 days)",
    12: "tribes / apostles",
    40: "testing (40 days/nights)",
    70: "weeks of Daniel",
    144: "144000 (Revelation 7), here as 144",
    153: "fish in the net (John 21:11)",
    666: "number of the beast (Rev 13:18)",
    777: "divine completeness",
    144000: "the sealed (Revelation 7:4)",
}
FIB_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786]  # classic retracement levels


def theory_layer(candles, tolerance_pct=0.5):
    """Scan price vs. esoteric reference levels. Returns coincidences within
    `tolerance_pct` of the latest close. CURIOSITY ONLY."""
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    last = closes[-1]
    swing_hi, swing_lo = max(highs), min(lows)
    rng = swing_hi - swing_lo

    levels = []  # (label, value, kind)

    # Fibonacci retracements between swing low and swing high
    for f in FIB_RATIOS:
        lvl = swing_hi - rng * f
        levels.append((f"Fib {f:.3f} retr.", lvl, "fibonacci"))

    # Golden ratio projections off the swing low
    levels.append(("Swing low x phi", swing_lo * PHI, "golden-ratio"))
    levels.append(("Swing low x phi^2", swing_lo * PHI * PHI, "golden-ratio"))

    # Biblical / Gann numbers scaled into price magnitude (x powers of 10)
    mag = 10 ** math.floor(math.log10(max(last, 1)))
    for num, meaning in BIBLICAL_NUMBERS.items():
        for scale in (mag / 100, mag / 10, mag, mag * 10):
            val = num * scale
            if swing_lo * 0.5 <= val <= swing_hi * 1.5:
                levels.append((f"{num} ({meaning})", val, "biblical"))

    # Find coincidences with the latest close
    hits = []
    for label, val, kind in levels:
        if val <= 0:
            continue
        dist = abs(last - val) / last * 100
        if dist <= tolerance_pct:
            hits.append({
                "label": label, "value": round(val, 4),
                "kind": kind, "distance_pct": round(dist, 4),
            })
    hits.sort(key=lambda h: h["distance_pct"])
    return {
        "swing_high": swing_hi, "swing_low": swing_lo,
        "tolerance_pct": tolerance_pct,
        "levels_scanned": len(levels),
        "hits": hits,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scout(symbols, interval, limit, demo, tolerance):
    results = []
    for sym in symbols:
        try:
            candles = synthetic_klines(sym) if demo else fetch_klines(sym, interval, limit)
            source = "SYNTHETIC (demo)" if demo else "Binance public API"
        except RuntimeError as e:
            print(f"[!] {sym}: {e}", file=sys.stderr)
            continue
        ta = ta_signals(candles)
        th = theory_layer(candles, tolerance)
        results.append({
            "symbol": sym, "interval": interval, "source": source,
            "candles": candles, "ta": ta, "theory": th,
        })
        verdict = "ALIGNMENT FOUND" if th["hits"] else "no alignment"
        print(f"[+] {sym}: last={ta['last']:.2f}  RSI={ta['rsi'] and round(ta['rsi'],1)}  "
              f"theory={verdict} ({len(th['hits'])} hit(s))")
    return results


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

def build_html(results, out_path):
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def spark(candles, w=560, h=120):
        closes = [c["close"] for c in candles][-180:]
        lo, hi = min(closes), max(closes)
        rng = (hi - lo) or 1
        pts = []
        for i, v in enumerate(closes):
            x = i / (len(closes) - 1) * w
            y = h - (v - lo) / rng * h
            pts.append(f"{x:.1f},{y:.1f}")
        return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
                f'style="width:100%;height:120px">'
                f'<polyline fill="none" stroke="#3fb950" stroke-width="2" '
                f'points="{" ".join(pts)}"/></svg>')

    cards = []
    for r in results:
        ta, th = r["ta"], r["theory"]
        ta_rows = "".join(
            f'<tr><td>{name}</td><td class="badge {dir.lower()}">{dir}</td><td>{desc}</td></tr>'
            for name, dir, desc in ta["findings"]
        )
        if th["hits"]:
            hit_rows = "".join(
                f'<tr><td>{h["label"]}</td><td>{h["value"]}</td>'
                f'<td><span class="kind {h["kind"]}">{h["kind"]}</span></td>'
                f'<td>{h["distance_pct"]}%</td></tr>'
                for h in th["hits"]
            )
            theory_block = (
                f'<table class="t"><thead><tr><th>Reference</th><th>Level</th>'
                f'<th>Theory</th><th>Δ from price</th></tr></thead><tbody>{hit_rows}</tbody></table>'
            )
            theory_summary = f'<b>{len(th["hits"])} coincidence(s)</b> within {th["tolerance_pct"]}% of price'
        else:
            theory_block = '<p class="muted">No price/level coincidence within tolerance.</p>'
            theory_summary = "no coincidence"

        cards.append(f"""
        <section class="card">
          <header>
            <h2>{r['symbol']} <small>{r['interval']}</small></h2>
            <span class="src">{r['source']}</span>
          </header>
          {spark(r['candles'])}
          <div class="price">Last: <b>{ta['last']:.2f}</b>
            &nbsp; SMA20: {ta['sma20'] and round(ta['sma20'],2)}
            &nbsp; SMA50: {ta['sma50'] and round(ta['sma50'],2)}
            &nbsp; RSI: {ta['rsi'] and round(ta['rsi'],1)}</div>

          <h3>Technical signals <span class="tag real">standard TA</span></h3>
          <table class="t"><tbody>{ta_rows}</tbody></table>

          <h3>Theory layer <span class="tag curio">curiosity only</span></h3>
          <p class="muted">Scanned {th['levels_scanned']} esoteric levels · {theory_summary}</p>
          {theory_block}
        </section>""")

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Signal Scout</title>
<style>
  :root {{ color-scheme: dark; }}
  body {{ margin:0; font:15px/1.5 system-ui,sans-serif; background:#0d1117; color:#e6edf3; }}
  header.top {{ padding:24px; border-bottom:1px solid #30363d; }}
  header.top h1 {{ margin:0; font-size:22px; }}
  .disclaimer {{ background:#21262d; border:1px solid #f0883e; color:#f0c674;
     padding:12px 16px; margin:16px 24px; border-radius:8px; font-size:13px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr));
     gap:18px; padding:24px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:12px; padding:18px; }}
  .card header {{ display:flex; justify-content:space-between; align-items:baseline; }}
  .card h2 {{ margin:0; }} .card h2 small {{ color:#8b949e; font-size:13px; }}
  .src {{ font-size:11px; color:#8b949e; }}
  .price {{ font-size:13px; color:#c9d1d9; margin:8px 0 4px; }}
  h3 {{ font-size:14px; margin:16px 0 6px; }}
  table.t {{ width:100%; border-collapse:collapse; font-size:13px; }}
  table.t td, table.t th {{ padding:5px 8px; border-bottom:1px solid #21262d; text-align:left; }}
  .badge {{ font-weight:600; }}
  .bullish, .oversold {{ color:#3fb950; }} .bearish, .overbought {{ color:#f85149; }}
  .neutral, .info {{ color:#8b949e; }}
  .tag {{ font-size:10px; padding:2px 7px; border-radius:10px; vertical-align:middle; }}
  .tag.real {{ background:#1f6feb33; color:#79c0ff; border:1px solid #1f6feb; }}
  .tag.curio {{ background:#f0883e33; color:#f0c674; border:1px solid #f0883e; }}
  .kind {{ font-size:11px; padding:1px 6px; border-radius:8px; }}
  .kind.fibonacci {{ background:#388bfd33; color:#79c0ff; }}
  .kind.biblical {{ background:#a371f733; color:#d2a8ff; }}
  .kind.golden-ratio {{ background:#e3b34133; color:#f0c674; }}
  .muted {{ color:#8b949e; font-size:12px; }}
  footer {{ padding:24px; color:#8b949e; font-size:12px; border-top:1px solid #30363d; }}
</style></head>
<body>
  <header class="top">
    <h1>🛰️ Crypto Signal Scout</h1>
    <div class="muted">Generated {generated} · {len(results)} symbol(s)</div>
  </header>
  <div class="disclaimer">
    <b>Read me.</b> The <b>Technical signals</b> block is standard technical analysis — not
    financial advice, no guaranteed predictive power. The <b>Theory layer</b> (Fibonacci /
    golden ratio / biblical numbers) is included as a <b>curiosity</b>: a price "lining up"
    with a sacred number is coincidence (apophenia), not a trading edge. Do not risk money on it.
  </div>
  <div class="grid">{''.join(cards)}</div>
  <footer>Built by crypto_signal_scout.py · Data: Binance public REST API (free, no key).
    Esoteric overlay is for entertainment. Nothing here is investment advice.</footer>
</body></html>"""


def main():
    ap = argparse.ArgumentParser(description="Free Binance data + TA + esoteric overlay → HTML dashboard")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    ap.add_argument("--interval", default="1d", help="1m,5m,15m,1h,4h,1d,1w ...")
    ap.add_argument("--limit", type=int, default=365, help="number of candles (max 1000)")
    ap.add_argument("--tolerance", type=float, default=0.75, help="theory-hit tolerance, %% of price")
    ap.add_argument("--out", default="crypto_dashboard.html")
    ap.add_argument("--demo", action="store_true", help="offline synthetic data (no network)")
    args = ap.parse_args()

    print(f"Scouting {args.symbols} @ {args.interval} "
          f"({'demo/offline' if args.demo else 'live Binance'}) ...")
    results = scout(args.symbols, args.interval, args.limit, args.demo, args.tolerance)
    if not results:
        print("No data fetched. Try --demo to generate an offline dashboard.", file=sys.stderr)
        sys.exit(1)
    html = build_html(results, args.out)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"\n✓ Dashboard written to {args.out}  (open it in a browser)")


if __name__ == "__main__":
    main()
