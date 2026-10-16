"""Signal assembly — turn four raw sources into one flat bundle per token.

A SignalBundle is deliberately plain data: it is what the deterministic scorer
reads, what the Claude analyst is shown, and what gets written to the audit
record. Same object everywhere, so what you backtest is what you traded.

Every field is Optional. Sources fail independently — a rate-limited RPC must
degrade the bundle, never abort the scan — so `missing` records exactly what
could not be fetched, and the scorer discounts confidence accordingly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from .sources import dexscreener, jupiter, rpc, smartmoney
from .pricelog import PriceLog

# TA needs real history before it means anything. Below this many bars the
# indicators are fitting noise, and krypt's scorer will happily return -100
# off a single component. Do not let that reach a trading decision.
MIN_TA_CANDLES = 60


@dataclass
class SignalBundle:
    mint: str
    symbol: str = None
    name: str = None
    ts: int = field(default_factory=lambda: int(time.time() * 1000))

    # DEX microstructure (dexscreener)
    micro: dict = None
    # On-chain safety facts (rpc)
    chain: dict = None
    # Execution reality (jupiter round-trip quote)
    execution: dict = None
    # Smart-money flow for this mint
    smart: dict = None
    # TA on locally accumulated price history
    ta: dict = None

    missing: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @property
    def price(self):
        if self.micro and self.micro.get("price_usd"):
            return self.micro["price_usd"]
        return None


def gather(cfg, mint, *, pairs=None, smart_agg=None, price_log=None):
    """Build one SignalBundle. Never raises on a source failure."""
    b = SignalBundle(mint=mint)
    log = price_log or PriceLog(cfg.state_dir)

    # --- 1. DEX microstructure --------------------------------------------
    try:
        ps = pairs if pairs is not None else dexscreener.pairs_for_mints(
            cfg.dexscreener_url, [mint])
        best = dexscreener.best_pair(ps, mint)
        b.micro = dexscreener.summarize_pair(best)
        if b.micro:
            b.symbol, b.name = b.micro.get("symbol"), b.micro.get("name")
            b.micro["pair_age_minutes"] = _age_minutes(b.micro.get("pair_created_at"))
            log.append(mint, b.micro.get("price_usd"),
                       volume=b.micro.get("volume_h24"))
        else:
            b.missing.append("micro:no_solana_pair")
    except Exception as e:
        b.missing.append(f"micro:{type(e).__name__}")

    # --- 2. On-chain safety ------------------------------------------------
    try:
        mint_info = rpc.parse_mint(cfg.rpc_url, mint)
        holders = rpc.largest_holders(cfg.rpc_url, mint)
        b.chain = {**mint_info, "holders": holders}
        if mint_info.get("has_token2022_extensions"):
            b.notes.append("Token-2022 extensions present: transfer fees or a "
                           "transfer hook could tax or block your exit.")
    except Exception as e:
        b.missing.append(f"chain:{type(e).__name__}")

    # --- 3. Execution reality ---------------------------------------------
    try:
        b.execution = jupiter.round_trip_cost(cfg.jupiter_url, mint,
                                              cfg.quote_size_usd)
    except Exception as e:
        b.missing.append(f"execution:{type(e).__name__}")

    # --- 4. Smart money ----------------------------------------------------
    if smart_agg is not None:
        b.smart = (smart_agg.get("mints") or {}).get(mint) or {
            "net_wallets": 0, "accumulating_wallets": 0,
            "distributing_wallets": 0, "total_delta": 0.0, "wallets": [],
        }
        b.smart["wallets_tracked"] = smart_agg.get("wallets_tracked", 0)
    elif cfg.smart_wallets:
        b.missing.append("smart:not_fetched")

    # --- 5. TA on local history -------------------------------------------
    try:
        candles = log.candles(mint, cfg.ta_interval_minutes, cfg.ta_lookback)
        b.ta = _ta(candles)
    except Exception as e:
        b.missing.append(f"ta:{type(e).__name__}")

    return b


def _ta(candles):
    """TA snapshot, explicitly marked cold when there is not enough history."""
    n = len(candles)
    if n < MIN_TA_CANDLES:
        return {"ready": False, "candles": n, "needed": MIN_TA_CANDLES,
                "score": None, "label": "COLD",
                "reason": f"only {n}/{MIN_TA_CANDLES} bars of local history"}
    from krypt import indicators
    from krypt.scoring import score_snapshot
    snap = indicators.compute_all(candles)["latest"]
    sc = score_snapshot(snap)
    return {"ready": True, "candles": n, "score": sc["score"],
            "label": sc["label"], "components": sc["components"],
            "latest": snap}


def _age_minutes(created_ms):
    if not created_ms:
        return None
    try:
        return (time.time() * 1000 - float(created_ms)) / 60_000.0
    except (TypeError, ValueError):
        return None


def gather_many(cfg, mints, *, price_log=None):
    """Batch the batchable calls, then assemble per mint.

    One DexScreener request covers 30 mints and one smart-money sweep covers
    every wallet, so both are hoisted out of the per-token loop. The RPC and
    quote calls are inherently per-token.
    """
    log = price_log or PriceLog(cfg.state_dir)
    pairs = []
    try:
        pairs = dexscreener.pairs_for_mints(cfg.dexscreener_url, mints)
    except Exception:
        pairs = []

    smart_agg = None
    if cfg.smart_wallets:
        try:
            smart_agg = smartmoney.aggregate(
                cfg.rpc_url, cfg.smart_wallets,
                max_txs=max(1, cfg.smart_lookback_sigs // 4))
        except Exception:
            smart_agg = None

    return [gather(cfg, m, pairs=pairs, smart_agg=smart_agg, price_log=log)
            for m in mints]
