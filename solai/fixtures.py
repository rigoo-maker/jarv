"""Deterministic synthetic fixtures for offline testing.

The Solana data hosts are unreachable from some sandboxes (and rate-limited
from everywhere), so the whole pipeline is exercised against these instead.
No randomness: a seeded LCG, so a failing smoke test fails identically twice.
"""

from __future__ import annotations

import time


class _Rand:
    def __init__(self, seed=42):
        self.s = seed

    def next(self):
        self.s = (1103515245 * self.s + 12345) & 0x7FFFFFFF
        return self.s / 0x7FFFFFFF


def pair(mint, symbol, *, liq=180_000, vol24=900_000, age_min=5000,
         chg_h1=8.0, chg_h24=40.0, buys_h1=310, sells_h1=190):
    created = int(time.time() * 1000 - age_min * 60_000)
    return {
        "chainId": "solana", "pairAddress": f"PAIR{symbol}", "dexId": "raydium",
        "url": f"https://dexscreener.com/solana/{mint}",
        "baseToken": {"address": mint, "symbol": symbol, "name": f"{symbol} Token"},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112",
                       "symbol": "SOL"},
        "priceUsd": "0.004213", "priceNative": "0.0000241",
        "liquidity": {"usd": liq, "base": 1e9, "quote": 400},
        "volume": {"m5": vol24 / 288, "h1": vol24 / 24, "h6": vol24 / 4, "h24": vol24},
        "priceChange": {"m5": 0.6, "h1": chg_h1, "h6": chg_h24 / 2, "h24": chg_h24},
        "txns": {"m5": {"buys": 12, "sells": 8},
                 "h1": {"buys": buys_h1, "sells": sells_h1},
                 "h24": {"buys": buys_h1 * 18, "sells": sells_h1 * 20}},
        "fdv": 4_200_000, "marketCap": 3_800_000, "pairCreatedAt": created,
    }


def mint_account(*, mint_auth=False, freeze=False, supply=10 ** 15, decimals=6,
                 token2022=False):
    import base64
    b = bytearray(82)
    b[0:4] = (1 if mint_auth else 0).to_bytes(4, "little")
    if mint_auth:
        b[4:36] = bytes([7]) * 32
    b[36:44] = supply.to_bytes(8, "little")
    b[44] = decimals
    b[45] = 1
    b[46:50] = (1 if freeze else 0).to_bytes(4, "little")
    if freeze:
        b[50:82] = bytes([9]) * 32
    owner = ("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb" if token2022
             else "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")
    return {"value": {"owner": owner, "lamports": 1461600,
                      "data": [base64.b64encode(bytes(b)).decode(), "base64"]}}


def holders(top1=6.0, top10=22.0, burned=88.0, supply=1_000_000_000.0):
    """Largest-accounts RPC payload with a burn address at the top."""
    rest = max(0.0, top10 - top1)
    accts = [{"address": "1nc1nerator11111111111111111111111111111111",
              "uiAmountString": str(supply * burned / 100)},
             {"address": "Whale1111111111111111111111111111111111111",
              "uiAmountString": str(supply * top1 / 100)}]
    for i in range(9):
        accts.append({"address": f"Holder{i:035d}",
                      "uiAmountString": str(supply * (rest / 9) / 100)})
    return {"value": accts}


def price_series(n=300, start=0.004, drift=0.0008, seed=7):
    """A rising-but-noisy series long enough to warm the TA engine."""
    r = _Rand(seed)
    base = (int(time.time() * 1000) - n * 60_000) // 300_000 * 300_000
    out, p = [], start
    for i in range(n):
        p *= 1 + drift + 0.02 * (r.next() - 0.5)
        out.append({"t": base + i * 60_000, "p": p, "v": 900_000})
    return out


ANALYST_OK = {
    "verdict": "confirm", "confidence": 0.71,
    "thesis": "Established pair with steady turnover and burned LP.",
    "red_flags": ["24h volume is 5x liquidity — verify it is not wash traded"],
    "missed_by_scorer": [], "injection_attempt_detected": False,
    "suggested_max_position_pct": 60.0,
}

ANALYST_VETO = {
    "verdict": "veto", "confidence": 0.88,
    "thesis": "Deep liquidity on a very young pair; move is 11 trades.",
    "red_flags": ["40% of the 24h move came from 11 trades"],
    "missed_by_scorer": ["top holders are sequential addresses"],
    "injection_attempt_detected": False, "suggested_max_position_pct": 0.0,
}
