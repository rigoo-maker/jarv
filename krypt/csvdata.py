"""CSV loader for arbitrary OHLCV files — Kaggle dumps, broker exports, NinjaTrader
or TradingView exports, anything with a date column and four prices.

The point is to get real data into the maps without hand-editing every file. Column
names, delimiters, date formats and row order all vary by source, so this sniffs
them instead of demanding one layout:

  * columns matched case-insensitively against alias lists (Date/Datetime/Timestamp,
    Open/O, Close/Adj Close/Last/Price, Volume/Vol/Qty, ...)
  * timestamps parsed from epoch seconds/millis, ISO 8601, YYYY-MM-DD, MM/DD/YYYY
    or DD/MM/YYYY — the ambiguous slash formats are resolved by testing the WHOLE
    column, not the first row, so 03/04 does not silently flip the calendar
  * numbers cleaned of $ , % and thousands separators
  * multi-symbol files filtered by a Symbol/Ticker column
  * rows with missing OHLC dropped, duplicates collapsed, everything sorted by time

Extra columns (VIX, rates, whatever the dataset ships) are ignored by the candle
path and returned separately, so they are there if you want to condition on them.
"""

from __future__ import annotations

import csv
import glob
import os
from datetime import datetime, timezone

ALIASES = {
    "time": ["time", "date", "datetime", "date_time", "timestamp", "open_time",
             "dt", "day", "index"],
    "open": ["open", "o", "open_price", "opening", "first"],
    "high": ["high", "h", "high_price", "max"],
    "low": ["low", "l", "low_price", "min"],
    # plain close wins over adjusted close; adjusted is the fallback
    "close": ["close", "c", "close_price", "closing", "close/last", "last",
              "adj close", "adj_close", "adjclose", "adjusted_close", "price"],
    "volume": ["volume", "vol", "v", "quantity", "qty", "total volume", "shares"],
    "symbol": ["symbol", "ticker", "instrument", "pair", "stock", "name"],
}

DATE_FORMATS = [
    "%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S", "%Y%m%d", "%Y%m%d %H:%M:%S",
    "%d-%m-%Y", "%m-%d-%Y", "%b %d, %Y", "%d %b %Y",
]
SLASH_FORMATS = ["%m/%d/%Y", "%d/%m/%Y", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
                 "%m/%d/%y", "%d/%m/%y"]


class CsvError(RuntimeError):
    pass


def _norm(name):
    return (name or "").strip().lower().replace("﻿", "")


def _map_columns(header):
    """field -> column index, by alias priority (earlier alias wins)."""
    cols = {_norm(h): i for i, h in enumerate(header)}
    out = {}
    for field, aliases in ALIASES.items():
        for alias in aliases:
            if alias in cols:
                out[field] = cols[alias]
                break
    return out


def _num(x):
    if x is None:
        return None
    s = str(x).strip().replace("$", "").replace(",", "").replace("%", "")
    if s in ("", "-", "null", "NULL", "None", "nan", "NaN", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_epoch(s):
    """Epoch seconds/millis/micros -> ms, or None if this isn't an epoch."""
    if not s.isdigit():
        return None
    v = int(s)
    if len(s) == 8:            # 20240102 is a date, not an epoch
        return None
    if v > 1e17:
        return v // 1_000_000
    if v > 1e14:
        return v // 1000
    if v > 1e11:
        return v
    if v > 1e8:
        return v * 1000
    return None


def _detect_date_format(samples):
    """Pick the format that parses EVERY sample. Ambiguous slash dates are decided
    by the whole column: if any row has day > 12 it settles month-vs-day order."""
    for fmt in DATE_FORMATS + SLASH_FORMATS:
        ok = True
        for s in samples:
            try:
                datetime.strptime(s, fmt)
            except ValueError:
                ok = False
                break
        if ok:
            return fmt
    return None


def _to_ms(s, fmt):
    if fmt == "__epoch__":
        return _parse_epoch(s)
    if fmt == "__iso__":
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    else:
        dt = datetime.strptime(s, fmt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def load_csv(path, symbol=None, *, quiet=False):
    """Return (candles, info). Candles are KRYPT dicts: time(ms), OHLC, volume."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(f, dialect))
    if len(rows) < 3:
        raise CsvError(f"{path}: fewer than 3 rows — not a data file?")
    header, body = rows[0], rows[1:]
    cmap = _map_columns(header)
    missing = [k for k in ("time", "open", "high", "low", "close") if k not in cmap]
    if missing:
        raise CsvError(f"{path}: could not find column(s) {missing}. "
                       f"Header was: {header[:12]}")

    # symbol filter
    if "symbol" in cmap:
        si = cmap["symbol"]
        syms = {r[si].strip() for r in body if len(r) > si and r[si].strip()}
        if symbol:
            want = symbol.strip().lower()
            body = [r for r in body if len(r) > si and r[si].strip().lower() == want]
            if not body:
                raise CsvError(f"{path}: symbol '{symbol}' not found. "
                               f"Available: {sorted(syms)[:15]}")
        elif len(syms) > 1:
            raise CsvError(f"{path} holds {len(syms)} symbols — pass one with "
                           f"--symbols. First few: {sorted(syms)[:15]}")

    ti = cmap["time"]
    raw_times = [r[ti].strip() for r in body if len(r) > ti and r[ti].strip()]
    if not raw_times:
        raise CsvError(f"{path}: time column is empty")
    probe = raw_times[:200]
    if all(_parse_epoch(s) is not None for s in probe):
        fmt = "__epoch__"
    else:
        fmt = _detect_date_format(probe)
        if fmt is None:
            try:
                for s in probe:
                    datetime.fromisoformat(s.replace("Z", "+00:00"))
                fmt = "__iso__"
            except ValueError:
                raise CsvError(f"{path}: unrecognized date format, e.g. {probe[0]!r}")

    extras = {_norm(h): i for i, h in enumerate(header)
              if _norm(h) not in {_norm(header[i2]) for k, i2 in cmap.items()}}
    candles, extra_rows, skipped = [], [], 0
    for r in body:
        try:
            if len(r) <= max(cmap.values()):
                skipped += 1
                continue
            t = _to_ms(r[ti].strip(), fmt)
            o, h, l, c = (_num(r[cmap[k]]) for k in ("open", "high", "low", "close"))
            if t is None or None in (o, h, l, c):
                skipped += 1
                continue
            v = _num(r[cmap["volume"]]) if "volume" in cmap else None
            candles.append({"time": t, "open": o, "high": h, "low": l, "close": c,
                            # volume 0 would make VWAP/OBV meaningless; 1.0 turns
                            # VWAP into an unweighted mean rather than a divide-by-zero
                            "volume": v if v else 1.0})
            extra_rows.append({k: _num(r[i]) for k, i in extras.items() if i < len(r)})
        except (ValueError, IndexError):
            skipped += 1

    if not candles:
        raise CsvError(f"{path}: no usable rows (skipped {skipped})")
    order = sorted(range(len(candles)), key=lambda i: candles[i]["time"])
    candles = [candles[i] for i in order]
    extra_rows = [extra_rows[i] for i in order]
    deduped, dext = [], []
    for c, e in zip(candles, extra_rows):          # keep the last row per timestamp
        if deduped and c["time"] == deduped[-1]["time"]:
            deduped[-1], dext[-1] = c, e
            continue
        deduped.append(c)
        dext.append(e)

    info = {
        "path": path, "rows": len(body), "candles": len(deduped),
        "skipped": skipped, "date_format": fmt,
        "columns": {k: header[i] for k, i in cmap.items()},
        "extra_columns": [header[i] for i in extras.values()],
        "no_volume": "volume" not in cmap,
        "t0": deduped[0]["time"] / 1000, "t1": deduped[-1]["time"] / 1000,
        "interval": infer_interval(deduped),
        "extras": dext,
    }
    if not quiet:
        print(f"  {os.path.basename(path)}: {len(deduped):,} candles "
              f"{datetime.utcfromtimestamp(info['t0']):%Y-%m-%d} -> "
              f"{datetime.utcfromtimestamp(info['t1']):%Y-%m-%d} "
              f"(inferred interval {info['interval']}, {skipped} rows skipped)")
        if info["extra_columns"]:
            print(f"  extra columns ignored by the candle path: "
                  f"{', '.join(info['extra_columns'][:10])}")
    return deduped, info


def infer_interval(candles):
    """Label the bar size from the MEDIAN gap — the mean is wrecked by weekends."""
    if len(candles) < 3:
        return "1d"
    gaps = sorted(candles[i]["time"] - candles[i - 1]["time"]
                  for i in range(1, len(candles)))
    med = gaps[len(gaps) // 2] / 1000.0
    for label, secs in (("1s", 1), ("1m", 60), ("3m", 180), ("5m", 300),
                        ("15m", 900), ("30m", 1800), ("1h", 3600), ("4h", 14400),
                        ("1d", 86400), ("1wk", 604800)):
        if med <= secs * 1.5:
            return label
    return "1mo"


def load(path, symbol=None, *, quiet=False):
    """Load a CSV file, or pick the right CSV out of a downloaded dataset folder."""
    if os.path.isfile(path):
        return load_csv(path, symbol, quiet=quiet)
    files = sorted(glob.glob(os.path.join(path, "**", "*.csv"), recursive=True) +
                   glob.glob(os.path.join(path, "**", "*.txt"), recursive=True))
    if not files:
        raise CsvError(f"{path}: no .csv/.txt files found")
    if symbol:
        named = [f for f in files
                 if symbol.lower() in os.path.basename(f).lower()]
        if named:
            files = named
    if len(files) > 1:
        biggest = max(files, key=os.path.getsize)
        if not quiet:
            print(f"  {len(files)} files in {path}; using the largest "
                  f"({os.path.basename(biggest)}). Pass the exact path to override.")
        files = [biggest]
    return load_csv(files[0], symbol, quiet=quiet)


def download_kaggle(handle, quiet=False):
    """Fetch a Kaggle dataset via kagglehub and return the local path.

    Optional dependency and needs Kaggle credentials + network egress to
    api.kaggle.com — both fail loudly rather than silently returning nothing.
    """
    try:
        import kagglehub
    except ImportError:
        raise CsvError("kagglehub is not installed — `pip install kagglehub`, or "
                       "download the dataset manually and pass --file <path>")
    if not quiet:
        print(f"Downloading Kaggle dataset {handle} ...")
    try:
        return kagglehub.dataset_download(handle)
    except Exception as e:                       # auth, network, or missing dataset
        raise CsvError(f"kagglehub could not fetch '{handle}': {e}\n"
                       "Check ~/.kaggle/kaggle.json (or KAGGLE_USERNAME/KAGGLE_KEY) "
                       "and that api.kaggle.com is reachable from this machine.")
