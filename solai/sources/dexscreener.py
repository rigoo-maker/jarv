"""DexScreener — DEX microstructure. Free, no API key.

Endpoints used (public, documented at docs.dexscreener.com):
  GET /latest/dex/tokens/{mints}     pairs for up to 30 comma-separated mints
  GET /latest/dex/search?q=          text search -> pairs
  GET /token-boosts/top/v1           tokens with paid promotion (a discovery
                                     surface AND a warning sign; see below)
  GET /token-profiles/latest/v1      recently profiled tokens

These shapes are not versioned by the vendor. Every accessor below is
defensive: a missing field yields None rather than a KeyError, so a shape
change degrades the score instead of crashing the scan.
"""

from __future__ import annotations

from ..http import get_json

CHAIN = "solana"


def _num(d, *path, default=None):
    """Walk a nested dict, coercing the leaf to float. None if absent/unparseable."""
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    if cur is None or isinstance(cur, (dict, list)):
        return default
    try:
        return float(cur)
    except (TypeError, ValueError):
        return default


def pairs_for_mints(base_url, mints, *, timeout=20.0):
    """Return every Solana pair for these mints, newest-liquidity first.

    DexScreener caps the path at 30 addresses, so we chunk.
    """
    out = []
    mints = [m for m in mints if m]
    for i in range(0, len(mints), 30):
        chunk = ",".join(mints[i:i + 30])
        data = get_json(f"{base_url}/latest/dex/tokens/{chunk}", timeout=timeout)
        for p in (data or {}).get("pairs") or []:
            if p.get("chainId") == CHAIN:
                out.append(p)
    return out


def token_boosts(base_url, *, timeout=20.0):
    """Tokens whose teams paid for promotion.

    Useful as a discovery feed, but read it correctly: a boost means someone
    spent money on attention, which is information about marketing spend, not
    about the asset. The scorer treats it as a mild negative.
    """
    data = get_json(f"{base_url}/token-boosts/top/v1", timeout=timeout)
    return [d for d in (data or []) if d.get("chainId") == CHAIN]


def token_profiles(base_url, *, timeout=20.0):
    data = get_json(f"{base_url}/token-profiles/latest/v1", timeout=timeout)
    return [d for d in (data or []) if d.get("chainId") == CHAIN]


def best_pair(pairs, mint):
    """The pair that actually matters for a mint: deepest liquidity where the
    mint is the BASE token. Quote-side appearances are someone else's trade."""
    cands = [p for p in pairs
             if (p.get("baseToken") or {}).get("address") == mint]
    if not cands:
        return None
    return max(cands, key=lambda p: _num(p, "liquidity", "usd", default=0.0) or 0.0)


def summarize_pair(p):
    """Flatten a DexScreener pair into the microstructure fields we score on."""
    if not p:
        return None
    txn = p.get("txns") or {}

    def side(window, key):
        v = (txn.get(window) or {}).get(key)
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    buys_h1, sells_h1 = side("h1", "buys"), side("h1", "sells")
    buys_h24, sells_h24 = side("h24", "buys"), side("h24", "sells")
    liq = _num(p, "liquidity", "usd")
    vol24 = _num(p, "volume", "h24")

    return {
        "pair_address": p.get("pairAddress"),
        "dex": p.get("dexId"),
        "url": p.get("url"),
        "symbol": (p.get("baseToken") or {}).get("symbol"),
        "name": (p.get("baseToken") or {}).get("name"),
        "mint": (p.get("baseToken") or {}).get("address"),
        "quote_symbol": (p.get("quoteToken") or {}).get("symbol"),
        "price_usd": _num(p, "priceUsd"),
        "liquidity_usd": liq,
        "fdv": _num(p, "fdv"),
        "market_cap": _num(p, "marketCap"),
        "volume_m5": _num(p, "volume", "m5"),
        "volume_h1": _num(p, "volume", "h1"),
        "volume_h6": _num(p, "volume", "h6"),
        "volume_h24": vol24,
        "change_m5": _num(p, "priceChange", "m5"),
        "change_h1": _num(p, "priceChange", "h1"),
        "change_h6": _num(p, "priceChange", "h6"),
        "change_h24": _num(p, "priceChange", "h24"),
        "buys_h1": buys_h1, "sells_h1": sells_h1,
        "buys_h24": buys_h24, "sells_h24": sells_h24,
        "pair_created_at": p.get("pairCreatedAt"),
        # Derived microstructure -------------------------------------------
        # Volume/liquidity is the single most informative ratio here: it says
        # how many times the pool turned over. Very high means either genuine
        # demand or wash trading, and the two look identical from outside.
        "vol_liq_ratio": (vol24 / liq) if (vol24 and liq) else None,
        "buy_pressure_h1": _pressure(buys_h1, sells_h1),
        "buy_pressure_h24": _pressure(buys_h24, sells_h24),
        "avg_trade_usd": (vol24 / (buys_h24 + sells_h24))
                         if (vol24 and buys_h24 is not None and sells_h24 is not None
                             and (buys_h24 + sells_h24) > 0) else None,
    }


def _pressure(buys, sells):
    """Buy share of trade count, -1 (all sells) .. +1 (all buys)."""
    if buys is None or sells is None:
        return None
    total = buys + sells
    if total <= 0:
        return None
    return (buys - sells) / total
