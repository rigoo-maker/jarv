"""Smart-money wallet tracking.

Given a watchlist of wallets, walk their recent transactions and compute the
net token-balance delta per mint. Wallets that are accumulating a mint are a
signal; wallets distributing into your buy are a much louder one.

Cost warning, stated plainly: this is the most RPC-expensive source in SOLAI.
Each wallet costs one getSignaturesForAddress plus one getTransaction per
signature examined. The public endpoint will rate-limit you almost immediately
— set SOLANA_RPC_URL to a paid provider before enabling this with more than a
couple of wallets, or keep max_txs low.

Where does the watchlist come from? SOLAI does not invent one. You supply it
(SOLAI_SMART_WALLETS) from your own research. A wallet list is itself the
edge here, and a borrowed list is a crowded one.
"""

from __future__ import annotations

from . import rpc


def wallet_token_flows(url, wallet, *, max_txs=25, timeout=25.0):
    """Net per-mint balance change for one wallet across its recent txs.

    Returns {mint: {"delta": float, "buys": int, "sells": int, "last_slot": int}}
    """
    flows = {}
    try:
        sigs = rpc.signatures_for(url, wallet, limit=max_txs, timeout=timeout)
    except Exception as e:
        return {"__error__": str(e)}

    for s in sigs[:max_txs]:
        if s.get("err"):
            continue                      # failed tx moved nothing
        sig = s.get("signature")
        if not sig:
            continue
        try:
            tx = rpc.get_transaction(url, sig, timeout=timeout)
        except Exception:
            continue
        for mint, delta in _token_deltas(tx, wallet).items():
            f = flows.setdefault(mint, {"delta": 0.0, "buys": 0, "sells": 0,
                                        "last_slot": 0})
            f["delta"] += delta
            if delta > 0:
                f["buys"] += 1
            elif delta < 0:
                f["sells"] += 1
            f["last_slot"] = max(f["last_slot"], s.get("slot") or 0)
    return flows


def _token_deltas(tx, wallet):
    """post - pre token balances for accounts owned by `wallet`."""
    meta = (tx or {}).get("meta") or {}
    pre = {_key(b): _amt(b) for b in (meta.get("preTokenBalances") or [])
           if b.get("owner") == wallet}
    post = {_key(b): _amt(b) for b in (meta.get("postTokenBalances") or [])
            if b.get("owner") == wallet}
    out = {}
    for key in set(pre) | set(post):
        mint = key[0]
        delta = (post.get(key) or 0.0) - (pre.get(key) or 0.0)
        if abs(delta) > 1e-12:
            out[mint] = out.get(mint, 0.0) + delta
    return out


def _key(b):
    return (b.get("mint"), b.get("accountIndex"))


def _amt(b):
    ui = (b.get("uiTokenAmount") or {})
    v = ui.get("uiAmountString") or ui.get("uiAmount")
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def aggregate(url, wallets, *, max_txs=25, timeout=25.0):
    """Roll every tracked wallet's flows into a per-mint verdict.

    net_wallets is the number of distinct wallets net-long minus net-short.
    That is deliberately a wallet count, not a dollar sum: one whale doubling
    down should not outvote five independent wallets, because five independent
    wallets is the actual signal.
    """
    per_mint, errors = {}, []
    for w in wallets:
        flows = wallet_token_flows(url, w, max_txs=max_txs, timeout=timeout)
        if "__error__" in flows:
            errors.append({"wallet": w, "error": flows["__error__"]})
            continue
        for mint, f in flows.items():
            m = per_mint.setdefault(mint, {
                "accumulating_wallets": 0, "distributing_wallets": 0,
                "total_delta": 0.0, "wallets": [],
            })
            if f["delta"] > 0:
                m["accumulating_wallets"] += 1
            elif f["delta"] < 0:
                m["distributing_wallets"] += 1
            m["total_delta"] += f["delta"]
            m["wallets"].append({"wallet": w, "delta": f["delta"]})

    for mint, m in per_mint.items():
        m["net_wallets"] = m["accumulating_wallets"] - m["distributing_wallets"]
    return {"mints": per_mint, "errors": errors, "wallets_tracked": len(wallets)}
