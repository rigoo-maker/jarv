"""Local price history -> candles.

There is no free, keyless OHLC endpoint for arbitrary Solana tokens. Rather
than pretend otherwise (or make you buy a Birdeye key on day one), SOLAI logs
every price it observes to disk and resamples that into candles.

The consequence is honest and worth stating: TA is COLD at first run. With a
5-minute bar and a 50-period EMA you need ~4 hours of scanning before the
indicator means anything. The scorer knows this — it zero-weights TA until
enough bars exist rather than scoring noise. Run `scan` on a loop to warm it.
"""

from __future__ import annotations

import json
import os
import time


class PriceLog:
    def __init__(self, state_dir=".solai"):
        self.dir = os.path.join(state_dir, "prices")
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, mint):
        safe = "".join(c for c in mint if c.isalnum())[:64]
        return os.path.join(self.dir, f"{safe}.jsonl")

    def append(self, mint, price, ts_ms=None, volume=None):
        if price is None or price <= 0:
            return
        rec = {"t": int(ts_ms if ts_ms is not None else time.time() * 1000),
               "p": float(price)}
        if volume is not None:
            rec["v"] = float(volume)
        with open(self._path(mint), "a") as f:
            f.write(json.dumps(rec) + "\n")

    def samples(self, mint, limit=None):
        path = self._path(mint)
        if not os.path.exists(path):
            return []
        out = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue        # a torn write should not kill the scan
        out.sort(key=lambda r: r["t"])
        return out[-limit:] if limit else out

    def candles(self, mint, interval_minutes=5, lookback=200):
        """Resample samples into OHLCV candles in krypt's candle format, so
        krypt.indicators and krypt.scoring work on them unchanged."""
        return resample(self.samples(mint), interval_minutes, lookback)


def resample(samples, interval_minutes=5, lookback=200):
    if not samples:
        return []
    bucket_ms = int(interval_minutes * 60_000)
    buckets = {}
    for s in samples:
        b = (int(s["t"]) // bucket_ms) * bucket_ms
        p, v = float(s["p"]), float(s.get("v") or 0.0)
        c = buckets.get(b)
        if c is None:
            buckets[b] = {"time": b, "open": p, "high": p, "low": p,
                          "close": p, "volume": v, "n": 1}
        else:
            c["high"] = max(c["high"], p)
            c["low"] = min(c["low"], p)
            c["close"] = p
            # Volume samples are point-in-time 24h totals, not per-bar flow;
            # carrying the max avoids summing the same volume repeatedly.
            c["volume"] = max(c["volume"], v)
            c["n"] += 1
    out = [buckets[k] for k in sorted(buckets)]
    return out[-lookback:] if lookback else out
