"""Deterministic signal scorer.

Produces a 0..100 conviction score for a token that has ALREADY passed the
safety screens, plus a per-component breakdown so every number is traceable.
This layer is the one that can be backtested — keep it deterministic, keep the
LLM out of it.

Two design choices worth defending:

1. Volume/liquidity turnover is scored as an inverted-U, not a ramp. A pool
   turning over 3x in a day is real interest; one turning over 60x is almost
   always wash trading, and rewarding it monotonically is how a scanner walks
   straight into manufactured volume.

2. The score is hurdle-adjusted. Every candidate is charged its own measured
   round-trip cost. A token needs enough expected move to clear what it
   actually costs to trade, so an expensive token must be proportionally more
   compelling than a cheap one to earn the same score.
"""

from __future__ import annotations

DEFAULT_WEIGHTS = {
    "momentum": 1.3,        # multi-window price change, short weighted more
    "buy_pressure": 1.1,    # buy vs sell trade counts
    "turnover": 1.0,        # volume/liquidity, inverted-U
    "depth": 0.8,           # absolute liquidity — capacity for our size
    "smart_money": 1.4,     # tracked wallets accumulating
    "ta": 0.9,              # krypt confluence score, only when warm
    "trade_size": 0.5,      # avg trade size — retail flow vs bot ping-pong
}


def _clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


def score(bundle, cfg, weights=None):
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    micro = bundle.micro or {}
    comps = []

    def add(name, signal, detail, *, weighted=True):
        comps.append({"name": name, "signal": round(signal, 4),
                      "weight": w.get(name, 1.0) if weighted else 0.0,
                      "detail": detail})

    # --- momentum ----------------------------------------------------------
    m5, h1, h6, h24 = (micro.get("change_m5"), micro.get("change_h1"),
                       micro.get("change_h6"), micro.get("change_h24"))
    parts = [(m5, 0.15, "5m"), (h1, 0.40, "1h"), (h6, 0.30, "6h"), (h24, 0.15, "24h")]
    have = [(v, wt, lab) for v, wt, lab in parts if v is not None]
    if have:
        total_w = sum(wt for _, wt, _ in have)
        blended = sum(_clamp(v / 25.0) * wt for v, wt, _ in have) / total_w
        add("momentum", blended,
            "price " + ", ".join(f"{lab} {v:+.1f}%" for v, _, lab in have))

    # --- buy pressure ------------------------------------------------------
    bp1, bp24 = micro.get("buy_pressure_h1"), micro.get("buy_pressure_h24")
    if bp1 is not None or bp24 is not None:
        vals = [(bp1, 0.7), (bp24, 0.3)]
        vals = [(v, wt) for v, wt in vals if v is not None]
        tw = sum(wt for _, wt in vals)
        sig = sum(_clamp(v * 2.0) * wt for v, wt in vals) / tw
        add("buy_pressure", sig,
            f"buy share 1h {bp1:+.2f}" if bp1 is not None else
            f"buy share 24h {bp24:+.2f}")

    # --- turnover (inverted U) --------------------------------------------
    vlr = micro.get("vol_liq_ratio")
    if vlr is not None:
        if vlr <= 0.2:
            sig, why = -0.6, "dead pool"
        elif vlr <= 3.0:
            sig, why = _clamp((vlr - 0.2) / 2.8), "healthy turnover"
        elif vlr <= 10.0:
            sig, why = 1.0 - (vlr - 3.0) / 7.0 * 0.8, "hot"
        elif vlr <= 30.0:
            sig, why = 0.2 - (vlr - 10.0) / 20.0 * 0.9, "suspiciously hot"
        else:
            sig, why = -0.9, "wash-trading pattern"
        add("turnover", sig, f"vol/liq {vlr:.1f}x ({why})")

    # --- depth -------------------------------------------------------------
    liq = micro.get("liquidity_usd")
    if liq:
        # Saturates: past ~$1M more depth stops being an edge, it is just safe.
        sig = _clamp((liq / 250_000.0) ** 0.5 - 0.6, -1.0, 1.0)
        add("depth", sig, f"liquidity ${liq:,.0f}")

    # --- smart money -------------------------------------------------------
    sm = bundle.smart or {}
    if sm.get("wallets_tracked"):
        net = sm.get("net_wallets", 0)
        sig = _clamp(net / 3.0)
        add("smart_money", sig,
            f"{sm.get('accumulating_wallets',0)} accumulating / "
            f"{sm.get('distributing_wallets',0)} distributing "
            f"of {sm['wallets_tracked']} tracked")

    # --- TA (only when warm) ----------------------------------------------
    ta = bundle.ta or {}
    if ta.get("ready") and ta.get("score") is not None:
        add("ta", _clamp(ta["score"] / 100.0),
            f"krypt confluence {ta['score']:+.0f} ({ta['label']}) "
            f"on {ta['candles']} bars")
    elif ta:
        add("ta", 0.0, f"TA cold: {ta.get('reason','insufficient history')}",
            weighted=False)

    # --- trade size --------------------------------------------------------
    ats = micro.get("avg_trade_usd")
    if ats:
        # Sub-$20 average trade on a pool with big volume is usually bots
        # ping-ponging; $50-$2000 looks like actual participants.
        if ats < 20:
            sig, why = -0.5, "tiny average trade (bot churn)"
        elif ats <= 2000:
            sig, why = 0.5, "retail-sized flow"
        else:
            sig, why = -0.2, "few large trades (one seller can end it)"
        add("trade_size", sig, f"avg trade ${ats:,.0f} ({why})")

    # --- weighted blend ----------------------------------------------------
    num = sum(c["signal"] * c["weight"] for c in comps)
    den = sum(c["weight"] for c in comps)
    raw = (num / den) if den else 0.0            # -1..+1
    base = (raw + 1.0) * 50.0                    # 0..100

    # --- hurdle adjustment -------------------------------------------------
    ex = bundle.execution or {}
    cost = ex.get("round_trip_cost_pct")
    hurdle_penalty = 0.0
    if cost is not None:
        # Charge 4 points per 1% of round-trip cost. A 3% token must be
        # meaningfully better than a 0.5% token to rank equally.
        hurdle_penalty = min(30.0, cost * 4.0)
    final = max(0.0, min(100.0, base - hurdle_penalty))

    # --- confidence --------------------------------------------------------
    conf = _confidence(bundle, comps)

    return {
        "mint": bundle.mint,
        "symbol": bundle.symbol,
        "score": round(final, 2),
        "base_score": round(base, 2),
        "hurdle_penalty": round(hurdle_penalty, 2),
        "round_trip_cost_pct": cost,
        "confidence": conf["confidence"],
        "confidence_detail": conf["detail"],
        "components": comps,
        "label": _label(final),
    }


def _confidence(bundle, comps):
    """How much of the picture did we actually get?

    A high score built on two working sources is not the same object as the
    same score built on five, and collapsing them into one number hides the
    difference that matters most.
    """
    weighted = [c for c in comps if c["weight"] > 0]
    have = {c["name"] for c in weighted}
    expected = {"momentum", "buy_pressure", "turnover", "depth"}
    coverage = len(expected & have) / len(expected)
    penalty = 0.12 * len(bundle.missing or [])
    if not (bundle.ta or {}).get("ready"):
        penalty += 0.10
    if not (bundle.smart or {}).get("wallets_tracked"):
        penalty += 0.05
    conf = max(0.0, min(1.0, coverage - penalty))
    bits = [f"{len(expected & have)}/{len(expected)} core signals"]
    if bundle.missing:
        bits.append(f"missing: {', '.join(bundle.missing)}")
    if not (bundle.ta or {}).get("ready"):
        bits.append("TA cold")
    if not (bundle.smart or {}).get("wallets_tracked"):
        bits.append("no smart-money watchlist")
    return {"confidence": round(conf, 2), "detail": "; ".join(bits)}


def _label(s):
    if s >= 75:
        return "STRONG"
    if s >= 60:
        return "CONSTRUCTIVE"
    if s >= 45:
        return "NEUTRAL"
    if s >= 30:
        return "WEAK"
    return "AVOID"
