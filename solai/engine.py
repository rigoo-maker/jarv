"""Orchestration — discover, gather, screen, score, review, decide.

The order is the safety model:

    discover -> gather -> SCREEN -> score -> analyst -> decide

Screening comes before scoring so a structurally unsafe token is never even
ranked, and the analyst runs last and only on the shortlist so LLM spend is
bounded by `analyst_top_n` regardless of how many candidates were scanned.
"""

from __future__ import annotations

import time

from . import safety, scoring, analyst as analyst_mod
from .signals import gather_many
from .sources import dexscreener, jupiter
from .pricelog import PriceLog


def discover(cfg):
    """Build the candidate list. Explicit watchlist wins; otherwise pull
    Jupiter's organic-score feed and DexScreener's boost feed and merge."""
    if cfg.mints:
        return list(dict.fromkeys(cfg.mints))[:cfg.discover_limit]

    seen, out = set(), []

    def add(mint, source):
        if mint and mint not in seen:
            seen.add(mint)
            out.append({"mint": mint, "source": source})

    try:
        for t in jupiter.top_organic(cfg.jupiter_url, "24h", cfg.discover_limit):
            add(t["mint"], "jupiter_organic")
    except Exception:
        pass
    try:
        for t in dexscreener.token_boosts(cfg.dexscreener_url):
            add(t.get("tokenAddress"), "dexscreener_boost")
    except Exception:
        pass

    return [c["mint"] for c in out[:cfg.discover_limit]]


def scan(cfg, mints=None, *, price_log=None, analyst_client=None,
         run_analyst=None):
    """Full pipeline. Returns a list of candidate result dicts, best first."""
    log = price_log or PriceLog(cfg.state_dir)
    mints = mints if mints is not None else discover(cfg)
    if not mints:
        return {"ts": int(time.time() * 1000), "candidates": [],
                "scanned": 0, "note": "no candidates discovered"}

    bundles = gather_many(cfg, mints, price_log=log)

    results = []
    for b in bundles:
        sc_safety = safety.screen(b, cfg.safety)
        entry = {
            "mint": b.mint,
            "symbol": b.symbol,
            "name": b.name,
            "price": b.price,
            "safety": sc_safety,
            "score": None,
            "analyst": None,
            "decision": None,
            "bundle": b,
        }
        if sc_safety["verdict"] == "PASS":
            entry["score"] = scoring.score(b, cfg)
        results.append(entry)

    # Rank: only PASS candidates can be ranked at all.
    passing = [r for r in results if r["score"]]
    passing.sort(key=lambda r: r["score"]["score"], reverse=True)

    # --- analyst on the shortlist only ------------------------------------
    want_analyst = cfg.analyst_enabled if run_analyst is None else run_analyst
    if want_analyst:
        shortlist = [r for r in passing
                     if r["score"]["score"] >= cfg.analyst_min_score
                     ][:cfg.analyst_top_n]
        for r in shortlist:
            r["analyst"] = analyst_mod.review(r["bundle"], r["score"], cfg,
                                              client=analyst_client)

    for r in results:
        r["decision"] = decide(r, cfg)

    rejected = [r for r in results if not r["score"]]
    ordered = passing + rejected
    return {
        "ts": int(time.time() * 1000),
        "scanned": len(mints),
        "passed_safety": len(passing),
        "candidates": ordered,
        "analyst_calls": sum(1 for r in results if r["analyst"]),
    }


def decide(entry, cfg):
    """Final gate. Every rejection carries a reason — no silent skips."""
    s = entry.get("safety") or {}
    if s.get("verdict") != "PASS":
        return {"action": "skip", "reason": f"safety {s.get('verdict')}: "
                                            f"{s.get('summary')}"}
    score = entry.get("score") or {}
    if score.get("score", 0) < cfg.analyst_min_score:
        return {"action": "watch",
                "reason": f"score {score.get('score')} below entry threshold "
                          f"{cfg.analyst_min_score}"}

    a = entry.get("analyst")
    if a:
        if a.get("injection_attempt_detected"):
            return {"action": "skip",
                    "reason": "token metadata contained text attempting to "
                              "instruct the analyst — strong bad-faith signal"}
        if a.get("verdict") == "veto":
            flags = "; ".join(a.get("red_flags") or []) or a.get("thesis", "")
            return {"action": "skip", "reason": f"analyst veto: {flags}"}
        if a.get("verdict") == "abstain" and a.get("_error"):
            # An unavailable analyst must not silently become an approval.
            return {"action": "watch",
                    "reason": f"analyst unavailable ({a.get('_error')}); "
                              f"scorer-only signals do not clear the bar alone"}

    size_pct = 100.0
    if a and a.get("suggested_max_position_pct") is not None:
        size_pct = max(0.0, min(100.0, float(a["suggested_max_position_pct"])))
        if size_pct <= 0:
            return {"action": "skip", "reason": "analyst sized position to zero"}

    return {
        "action": "buy",
        "reason": f"score {score.get('score')} ({score.get('label')}), "
                  f"confidence {score.get('confidence')}"
                  + (f", analyst {a.get('verdict')}" if a else ""),
        "size_pct_of_cap": size_pct,
    }


def run_paper(cfg, portfolio, mints=None, **kw):
    """One paper-trading cycle: mark, exit, scan, enter."""
    result = scan(cfg, mints, **kw)
    prices = {r["mint"]: r["price"] for r in result["candidates"] if r["price"]}
    # Positions we hold but did not scan this cycle still need a price mark.
    for mint in list(portfolio.positions):
        if mint not in prices:
            try:
                px = jupiter.prices(cfg.jupiter_url, [mint]).get(mint, {}).get("price")
                if px:
                    prices[mint] = px
            except Exception:
                pass

    exits = portfolio.check_exits(prices)
    entries = []
    for r in result["candidates"]:
        d = r["decision"] or {}
        if d.get("action") != "buy" or not r["price"]:
            continue
        res = portfolio.open(r["mint"], r["symbol"] or r["mint"][:6], r["price"],
                             score=(r["score"] or {}).get("score"),
                             verdict=(r["analyst"] or {}).get("verdict"),
                             prices=prices)
        entries.append({"mint": r["mint"], "symbol": r["symbol"], **res})
        if not res.get("opened"):
            break                # portfolio is full or halted; stop trying
    equity = portfolio.mark(prices)
    return {**result, "exits": exits, "entries": entries, "equity": equity,
            "stats": portfolio.stats(prices)}
