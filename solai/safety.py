"""Hard safety screens — the veto layer.

Runs BEFORE scoring and is not a score. A token either passes or it does not;
no amount of momentum buys its way past a live mint authority. This ordering is
the whole point: on Solana small caps, the dominant loss mode is not a bad
entry, it is a token that could never have been exited.

Failures are split into two kinds:
  fatal   — structural. Mint authority live, no sell route, LP not burned.
  caution — quantitative and tunable. Thin liquidity, young pair, concentration.

`unknown` is tracked separately and treated as failure by default: a screen
that could not be evaluated has NOT passed. Silently treating unknown as safe
is how a rate-limited RPC turns into a rug.
"""

from __future__ import annotations


def screen(bundle, limits):
    fatal, caution, unknown, passed = [], [], [], []
    caveats = []

    micro = bundle.micro or {}
    chain = bundle.chain or {}
    holders = (chain.get("holders") or {})
    ex = bundle.execution or {}

    def check(name, value, ok, detail, kind="caution"):
        if value is None:
            unknown.append({"check": name, "detail": f"{detail} (not available)"})
        elif ok:
            passed.append({"check": name, "detail": detail})
        else:
            (fatal if kind == "fatal" else caution).append(
                {"check": name, "detail": detail})

    # --- structural / fatal ------------------------------------------------
    if limits.require_mint_authority_revoked:
        check("mint_authority_revoked", chain.get("mint_authority_revoked"),
              chain.get("mint_authority_revoked") is True,
              "mint authority revoked (supply cannot be inflated)"
              if chain.get("mint_authority_revoked")
              else f"MINT AUTHORITY LIVE ({chain.get('mint_authority')}) — "
                   f"deployer can mint unlimited supply",
              kind="fatal")

    if limits.require_freeze_authority_revoked:
        check("freeze_authority_revoked", chain.get("freeze_authority_revoked"),
              chain.get("freeze_authority_revoked") is True,
              "freeze authority revoked (your account cannot be frozen)"
              if chain.get("freeze_authority_revoked")
              else f"FREEZE AUTHORITY LIVE ({chain.get('freeze_authority')}) — "
                   f"deployer can freeze your tokens in place",
              kind="fatal")

    # A token you can buy but not sell is the purest form of loss.
    if ex:
        tradeable = ex.get("tradeable")
        check("sell_route_exists", tradeable, tradeable is True,
              "round-trip route confirmed" if tradeable
              else f"NO ROUND TRIP: {ex.get('reason')}",
              kind="fatal")

    if chain.get("is_spl_token") is False:
        fatal.append({"check": "token_program",
                      "detail": f"mint not owned by an SPL Token program "
                                f"(owner {chain.get('owner_program')})"})
    if chain.get("exists") is False:
        fatal.append({"check": "mint_exists",
                      "detail": "mint account does not exist on chain"})

    lp = chain.get("lp")
    if limits.require_lp_burned and lp is not None:
        check("lp_burned", lp.get("safe"), lp.get("safe") is True,
              "LP burned and LP mint authority revoked" if lp.get("safe")
              else f"LP NOT SECURED (supply {lp.get('lp_supply')}, "
                   f"mint auth revoked {lp.get('lp_mint_authority_revoked')}) — "
                   f"liquidity can be pulled",
              kind="fatal")
    elif limits.require_lp_burned:
        unknown.append({"check": "lp_burned",
                        "detail": "LP mint not resolved — cannot verify the "
                                  "pool is unpullable"})
    elif lp is None:
        # Not a screen result: a standing warning that one real risk is
        # unmeasured. It rides on every PASS so it cannot be forgotten.
        caveats.append("LP burn NOT verified — SOLAI cannot resolve the LP "
                       "mint automatically. Check it manually (a rug-checker "
                       "or the pool account) before entering.")

    # --- quantitative / caution -------------------------------------------
    liq = micro.get("liquidity_usd")
    check("min_liquidity", liq, (liq or 0) >= limits.min_liquidity_usd,
          f"liquidity ${liq:,.0f} vs floor ${limits.min_liquidity_usd:,.0f}"
          if liq is not None else "liquidity")

    vol = micro.get("volume_h24")
    check("min_volume", vol, (vol or 0) >= limits.min_volume_24h_usd,
          f"24h volume ${vol:,.0f} vs floor ${limits.min_volume_24h_usd:,.0f}"
          if vol is not None else "24h volume")

    age = micro.get("pair_age_minutes")
    check("min_pair_age", age, (age or 0) >= limits.min_pair_age_minutes,
          f"pair age {age:,.0f}m vs floor {limits.min_pair_age_minutes:,.0f}m"
          if age is not None else "pair age")
    if limits.max_pair_age_days > 0 and age is not None:
        cap = limits.max_pair_age_days * 1440
        check("max_pair_age", age, age <= cap,
              f"pair age {age:,.0f}m vs cap {cap:,.0f}m")

    top1 = holders.get("top1_pct_ex_burn")
    check("max_single_holder", top1, (top1 or 0) <= limits.max_single_holder_pct,
          f"largest non-burn holder {top1:.1f}% vs cap "
          f"{limits.max_single_holder_pct:.1f}%" if top1 is not None
          else "largest holder")

    top10 = holders.get("top10_pct_ex_burn")
    check("max_top10", top10, (top10 or 0) <= limits.max_top10_holder_pct,
          f"top-10 non-burn holders {top10:.1f}% vs cap "
          f"{limits.max_top10_holder_pct:.1f}%" if top10 is not None
          else "top-10 holders")

    impact = ex.get("round_trip_cost_pct") if ex.get("tradeable") else None
    check("max_price_impact", impact, (impact or 0) <= limits.max_price_impact_pct,
          f"round-trip cost {impact:.2f}% on ${ex.get('usd_size', 0):,.0f} "
          f"vs cap {limits.max_price_impact_pct:.2f}%" if impact is not None
          else "round-trip cost")

    ok = not fatal and not caution and not unknown
    return {
        "ok": ok,
        "fatal": fatal,
        "caution": caution,
        "unknown": unknown,
        "passed": passed,
        "caveats": caveats,
        "verdict": ("REJECT" if fatal else
                    "REJECT" if caution else
                    "INCONCLUSIVE" if unknown else "PASS"),
        "summary": _summary(fatal, caution, unknown, passed),
    }


def _summary(fatal, caution, unknown, passed):
    if fatal:
        return f"FATAL: {fatal[0]['detail']}"
    if caution:
        return f"failed {len(caution)} screen(s): " + \
               "; ".join(c["check"] for c in caution)
    if unknown:
        return f"{len(unknown)} screen(s) unevaluated: " + \
               "; ".join(u["check"] for u in unknown)
    return f"all {len(passed)} screens passed"
