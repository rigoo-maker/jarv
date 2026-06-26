"""Historical data loader for KRYPT.

Raw tick data for 3+ weeks of BTC is hundreds of millions of rows — do NOT try to
page that through the REST API. The right free source is Binance's bulk data
portal: https://data.binance.vision , which serves daily CSV dumps you download
once and analyze locally.

This module downloads and loads:
  - klines      (recommended: interval="1s" for HF, "1m" for normal TA)
  - aggTrades   (true tick/trade level — large, use only if you need it)

and can resample raw aggTrades into candles of any bucket size.

Everything is stdlib (urllib + zipfile + csv) and honors HTTPS_PROXY. If your
network blocks data.binance.vision you'll get a clear error — run where it's
reachable.
"""

from __future__ import annotations

import csv
import io
import os
import urllib.request
import urllib.error
import zipfile
from datetime import date, timedelta

VISION = "https://data.binance.vision/data/spot/daily"


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _download_zip(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "krypt/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url} "
                           f"(no data for that day/type?)")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach data.binance.vision (blocked/down?): {e}")


def kline_url(symbol, interval, day: date) -> str:
    return (f"{VISION}/klines/{symbol}/{interval}/"
            f"{symbol}-{interval}-{day.isoformat()}.zip")


def aggtrades_url(symbol, day: date) -> str:
    return f"{VISION}/aggTrades/{symbol}/{symbol}-aggTrades-{day.isoformat()}.zip"


def _read_single_csv(zbytes: bytes):
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8")
            for row in csv.reader(text):
                yield row


def load_klines_day(symbol, interval, day: date, cache_dir="data"):
    """Return list of candle dicts for one day from a klines dump."""
    os.makedirs(cache_dir, exist_ok=True)
    cache = os.path.join(cache_dir, f"{symbol}-{interval}-{day.isoformat()}.zip")
    if os.path.exists(cache):
        zbytes = open(cache, "rb").read()
    else:
        zbytes = _download_zip(kline_url(symbol, interval, day))
        with open(cache, "wb") as f:
            f.write(zbytes)
    candles = []
    for row in _read_single_csv(zbytes):
        # open_time, open, high, low, close, volume, close_time, ...
        try:
            candles.append({
                "time": int(row[0]) if len(row[0]) <= 13 else int(row[0]) // 1000,
                "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, IndexError):
            continue  # header row or malformed
    return candles


def download_klines_range(symbol, interval, start: date, end: date, cache_dir="data"):
    """Download+concatenate klines across [start, end]. Returns candle list."""
    out = []
    for d in _daterange(start, end):
        try:
            day = load_klines_day(symbol, interval, d, cache_dir)
            out.extend(day)
            print(f"  {d}  +{len(day)} candles (total {len(out)})")
        except RuntimeError as e:
            print(f"  {d}  skipped: {e}")
    out.sort(key=lambda c: c["time"])
    return out


def load_aggtrades_day(symbol, day: date, cache_dir="data"):
    """Return list of ticks {time, price, qty, is_buyer_maker} for one day.
    WARNING: this can be tens of millions of rows for BTC."""
    zbytes = _download_zip(aggtrades_url(symbol, day))
    ticks = []
    for row in _read_single_csv(zbytes):
        # aggId, price, qty, firstId, lastId, timestamp, isBuyerMaker, isBestMatch
        try:
            ticks.append({
                "time": int(row[5]), "price": float(row[1]),
                "qty": float(row[2]),
                "is_buyer_maker": str(row[6]).lower() in ("true", "1"),
            })
        except (ValueError, IndexError):
            continue
    return ticks


def ticks_to_candles(ticks, bucket_secs=1):
    """Resample raw ticks into OHLCV candles of `bucket_secs`."""
    if not ticks:
        return []
    ticks = sorted(ticks, key=lambda t: t["time"])
    bucket_ms = bucket_secs * 1000
    candles = []
    cur = None
    for t in ticks:
        b = (t["time"] // bucket_ms) * bucket_ms
        p = t["price"]
        if cur is None or cur["time"] != b:
            if cur is not None:
                candles.append(cur)
            cur = {"time": b, "open": p, "high": p, "low": p, "close": p,
                   "volume": t["qty"]}
        else:
            cur["high"] = max(cur["high"], p)
            cur["low"] = min(cur["low"], p)
            cur["close"] = p
            cur["volume"] += t["qty"]
    if cur is not None:
        candles.append(cur)
    return candles
