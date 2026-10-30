"""Solana JSON-RPC — on-chain safety facts.

This module answers the questions that actually decide whether a small account
loses money on Solana. Not "is the chart good" but:

  * Can the deployer still mint more supply?     (mint authority)
  * Can the deployer freeze your token account?  (freeze authority)
  * Is the LP burned, or can it be pulled?       (LP token supply/authority)
  * Does one wallet hold enough to nuke the bid? (holder concentration)

Everything here is derived from raw account data, not from a vendor's
"is it a rug" boolean. That matters: vendor rug-checkers are gameable and
opaque, and you cannot backtest a black box.
"""

from __future__ import annotations

import base64

from ..http import post_json
from ..config import TOKEN_PROGRAM, TOKEN_2022_PROGRAM

# Addresses that legitimately hold a large share of supply and must NOT be
# counted as concentration risk. Burn addresses and the incinerator are the
# common ones; AMM vaults are detected structurally instead (owner == a known
# AMM authority is not reliable, so we report both raw and adjusted figures).
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


def _rpc(url, method, params, *, timeout=20.0):
    resp = post_json(url, {"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}, timeout=timeout)
    if isinstance(resp, dict) and "error" in resp:
        raise RuntimeError(f"RPC {method} error: {resp['error']}")
    return (resp or {}).get("result")


def get_account_info(url, pubkey, *, timeout=20.0):
    return _rpc(url, "getAccountInfo",
                [pubkey, {"encoding": "base64", "commitment": "confirmed"}],
                timeout=timeout)


def parse_mint(url, mint, *, timeout=20.0):
    """Decode the SPL Token mint account.

    Layout (82 bytes, spl-token Mint):
        0..4    COption tag for mint_authority (1 = present)
        4..36   mint_authority pubkey
        36..44  supply (u64 LE)
        44      decimals (u8)
        45      is_initialized (u8)
        46..50  COption tag for freeze_authority
        50..82  freeze_authority pubkey

    Token-2022 mints share this prefix and append TLV extensions past byte 82;
    the prefix fields we read stay valid, and we flag that extensions exist
    because transfer-fee and transfer-hook extensions can tax or block a sale.
    """
    res = get_account_info(url, mint, timeout=timeout)
    val = (res or {}).get("value")
    if not val:
        return {"exists": False, "mint": mint}

    owner = val.get("owner")
    data_field = val.get("data")
    raw = b""
    if isinstance(data_field, list) and data_field:
        try:
            raw = base64.b64decode(data_field[0])
        except Exception:
            raw = b""

    out = {
        "exists": True,
        "mint": mint,
        "owner_program": owner,
        "is_token_2022": owner == TOKEN_2022_PROGRAM,
        "is_spl_token": owner in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM),
        "data_len": len(raw),
        "has_token2022_extensions": owner == TOKEN_2022_PROGRAM and len(raw) > 82,
    }
    if len(raw) < 82:
        out["parse_ok"] = False
        return out

    mint_auth_tag = int.from_bytes(raw[0:4], "little")
    supply = int.from_bytes(raw[36:44], "little")
    decimals = raw[44]
    initialized = bool(raw[45])
    freeze_tag = int.from_bytes(raw[46:50], "little")

    out.update({
        "parse_ok": True,
        "mint_authority": _b58(raw[4:36]) if mint_auth_tag == 1 else None,
        "mint_authority_revoked": mint_auth_tag == 0,
        "freeze_authority": _b58(raw[50:82]) if freeze_tag == 1 else None,
        "freeze_authority_revoked": freeze_tag == 0,
        "raw_supply": supply,
        "decimals": decimals,
        "supply": supply / (10 ** decimals) if decimals is not None else None,
        "is_initialized": initialized,
    })
    return out


def largest_holders(url, mint, *, timeout=20.0):
    """Top 20 token accounts by balance (all the RPC will give you).

    Returns raw percentages AND a version with burn addresses removed. Read the
    adjusted number: a token whose 'top holder' is the incinerator is the
    opposite of concentrated.
    """
    res = _rpc(url, "getTokenLargestAccounts",
               [mint, {"commitment": "confirmed"}], timeout=timeout)
    accounts = (res or {}).get("value") or []
    sup = _rpc(url, "getTokenSupply", [mint, {"commitment": "confirmed"}],
               timeout=timeout)
    supply_ui = _f(((sup or {}).get("value") or {}).get("uiAmountString"))
    if not supply_ui:
        supply_ui = _f(((sup or {}).get("value") or {}).get("uiAmount"))

    holders = []
    for a in accounts:
        amt = _f(a.get("uiAmountString")) or _f(a.get("uiAmount")) or 0.0
        addr = a.get("address")
        holders.append({
            "address": addr,
            "amount": amt,
            "pct": (amt / supply_ui * 100.0) if supply_ui else None,
            "is_burn": addr in BURN_ADDRESSES,
        })
    holders.sort(key=lambda h: h["amount"], reverse=True)

    live = [h for h in holders if not h["is_burn"]]
    return {
        "supply": supply_ui,
        "holders": holders,
        "top1_pct": holders[0]["pct"] if holders else None,
        "top10_pct": _sum_pct(holders[:10]),
        # Adjusted = burn addresses excluded. This is the number to screen on.
        "top1_pct_ex_burn": live[0]["pct"] if live else None,
        "top10_pct_ex_burn": _sum_pct(live[:10]),
        "burned_pct": _sum_pct([h for h in holders if h["is_burn"]]),
        "holder_sample": len(holders),
    }


def lp_status(url, lp_mint, *, timeout=20.0):
    """Is the LP token burned?

    An LP mint whose supply is ~0 means the liquidity provider tokens were
    destroyed and the pool cannot be withdrawn — the standard 'LP burned'
    claim, verified rather than trusted. A live mint authority on the LP mint
    is equally fatal: new LP tokens can be minted and the pool drained.
    """
    info = parse_mint(url, lp_mint, timeout=timeout)
    if not info.get("exists") or not info.get("parse_ok"):
        return {"lp_mint": lp_mint, "known": False}
    supply = info.get("supply") or 0.0
    return {
        "lp_mint": lp_mint,
        "known": True,
        "lp_supply": supply,
        "lp_burned": supply <= 1e-9,
        "lp_mint_authority_revoked": info.get("mint_authority_revoked"),
        "safe": bool(supply <= 1e-9 and info.get("mint_authority_revoked")),
    }


def signatures_for(url, address, limit=100, *, timeout=20.0):
    return _rpc(url, "getSignaturesForAddress",
                [address, {"limit": int(limit), "commitment": "confirmed"}],
                timeout=timeout) or []


def get_transaction(url, signature, *, timeout=25.0):
    return _rpc(url, "getTransaction", [signature, {
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0,
        "commitment": "confirmed",
    }], timeout=timeout)


# --- helpers ---------------------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(raw: bytes) -> str:
    """Base58-encode 32 raw bytes (stdlib only; no external base58 dep)."""
    n = int.from_bytes(raw, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    # Leading zero bytes each encode as '1'. When the value is all zeros the
    # padding IS the whole string — appending a fallback '1' would make it
    # 33 chars and no longer match the system program address.
    return "1" * pad + out


def _sum_pct(hs):
    vals = [h["pct"] for h in hs if h.get("pct") is not None]
    return sum(vals) if vals else None


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
