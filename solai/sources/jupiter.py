"""Jupiter — pricing, discovery, and *real* execution cost.

Endpoints (lite-api.jup.ag, keyless tier; swap out for the paid host via
SOLAI_JUPITER_URL if you hit rate limits):
  GET /price/v3?ids=<mints>                    spot USD price
  GET /tokens/v2/toporganicscore/{h1|h24|h7d}  discovery, organic-score ranked
  GET /swap/v1/quote?...                       routed quote -> real price impact

The quote endpoint is the important one and the reason we do not trust
DexScreener's liquidity number alone. Pool liquidity is a stock; what you care
about is the impact of *your* order routed through whatever path actually
exists. A $25 order that moves the price 4% tells you more than a $120k TVL
figure does.
"""

from __future__ import annotations

from ..http import get_json
from ..config import USDC


def prices(base_url, mints, *, timeout=20.0):
    """{mint: {"price": float, "decimals": int, "change24h": float|None}}"""
    out = {}
    mints = [m for m in mints if m]
    for i in range(0, len(mints), 50):
        chunk = ",".join(mints[i:i + 50])
        data = get_json(f"{base_url}/price/v3", params={"ids": chunk}, timeout=timeout)
        for mint, v in (data or {}).items():
            if not isinstance(v, dict):
                continue
            out[mint] = {
                "price": _f(v.get("usdPrice")),
                "decimals": v.get("decimals"),
                "change24h": _f(v.get("priceChange24h")),
            }
    return out


def top_organic(base_url, interval="24h", limit=30, *, timeout=20.0):
    """Discovery feed ranked by Jupiter's organic-score heuristic.

    Organic score is Jupiter's own attempt to separate real trading from wash
    volume. It is a vendor heuristic, not ground truth — we use it to build a
    candidate list, then re-derive everything ourselves.
    """
    if interval not in ("h1", "24h", "7d", "h24"):
        interval = "24h"
    data = get_json(f"{base_url}/tokens/v2/toporganicscore/{interval}",
                    params={"limit": int(limit)}, timeout=timeout)
    out = []
    for t in (data or []):
        if not isinstance(t, dict):
            continue
        mint = t.get("id") or t.get("address") or t.get("mint")
        if not mint:
            continue
        out.append({
            "mint": mint,
            "symbol": t.get("symbol"),
            "name": t.get("name"),
            "organic_score": _f(t.get("organicScore")),
            "holder_count": t.get("holderCount"),
            "liquidity": _f(t.get("liquidity")),
            "is_verified": bool(t.get("isVerified")),
        })
    return out


def quote(base_url, input_mint, output_mint, amount_atomic, *,
          slippage_bps=100, timeout=20.0):
    """One routed quote. Returns None if no route exists (itself a signal)."""
    try:
        data = get_json(f"{base_url}/swap/v1/quote", timeout=timeout, params={
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount_atomic),
            "slippageBps": int(slippage_bps),
        })
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("outAmount"):
        return None
    return {
        "in_amount": _f(data.get("inAmount")),
        "out_amount": _f(data.get("outAmount")),
        "price_impact_pct": _pct(data.get("priceImpactPct")),
        "route_hops": len(data.get("routePlan") or []),
        "route_labels": [((h.get("swapInfo") or {}).get("label"))
                         for h in (data.get("routePlan") or [])],
    }


def round_trip_cost(base_url, mint, usd_size, *, timeout=20.0):
    """Buy $usd_size of `mint` with USDC, then sell it all back. Report the
    real cost of the round trip in percent.

    This is the number that decides whether a small account can trade a token
    at all. If a $25 in-and-out costs 6%, no signal you have is worth 6%.
    """
    usdc_atomic = int(round(usd_size * 1_000_000))   # USDC has 6 decimals
    buy = quote(base_url, USDC, mint, usdc_atomic, timeout=timeout)
    if not buy or not buy["out_amount"]:
        return {"tradeable": False, "reason": "no buy route",
                "round_trip_cost_pct": None, "buy": None, "sell": None}
    sell = quote(base_url, mint, USDC, int(buy["out_amount"]), timeout=timeout)
    if not sell or not sell["out_amount"]:
        return {"tradeable": False, "reason": "no sell route (honeypot risk)",
                "round_trip_cost_pct": None, "buy": buy, "sell": None}
    returned = sell["out_amount"] / 1_000_000.0
    cost_pct = (usd_size - returned) / usd_size * 100.0
    return {
        "tradeable": True,
        "reason": None,
        "usd_size": usd_size,
        "usd_returned": returned,
        "round_trip_cost_pct": cost_pct,
        "buy_impact_pct": buy["price_impact_pct"],
        "sell_impact_pct": sell["price_impact_pct"],
        "buy": buy, "sell": sell,
    }


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct(v):
    """Jupiter returns priceImpactPct as a FRACTION ('0.0123' = 1.23%)."""
    f = _f(v)
    return None if f is None else f * 100.0
