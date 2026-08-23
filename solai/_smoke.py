"""Offline end-to-end smoke test. No network, no API key.

    python3 -m solai._smoke

Exercises: base58, mint parsing, safety screens (pass + every rejection),
scoring, TA warm-up, the analyst contract (via a stub), decision logic,
paper execution, persistence, and the live locks.
"""

from __future__ import annotations

import json
import sys
import tempfile

from . import fixtures as fx
from . import safety, scoring, venue, engine
from .config import load_config, SafetyLimits
from .paper import Portfolio
from .pricelog import PriceLog
from .signals import SignalBundle, gather_many
from .sources import rpc, dexscreener, jupiter

FAILS = []


def check(name, cond, detail=""):
    status = "ok  " if cond else "FAIL"
    print(f"  [{status}] {name}{(' — ' + detail) if detail else ''}")
    if not cond:
        FAILS.append(name)


def install_stubs(cfg, *, mint_auth=False, freeze=False, tradeable=True,
                  liq=180_000, age_min=5000, top1=6.0):
    """Point every source at fixtures instead of the network."""
    dexscreener.get_json = lambda *a, **k: {"pairs": [
        fx.pair("MintAAA", "AAA", liq=liq, age_min=age_min),
        fx.pair("MintBBB", "BBB", liq=42_000, age_min=900, chg_h24=-30.0,
                buys_h1=90, sells_h1=260),
    ]}

    def fake_rpc(url, method, params, timeout=20.0):
        if method == "getAccountInfo":
            return fx.mint_account(mint_auth=mint_auth, freeze=freeze)
        if method == "getTokenLargestAccounts":
            return fx.holders(top1=top1)
        if method == "getTokenSupply":
            return {"value": {"uiAmountString": "1000000000"}}
        return None
    rpc._rpc = fake_rpc
    rpc.get_account_info = lambda u, p, timeout=20.0: fx.mint_account(
        mint_auth=mint_auth, freeze=freeze)

    def fake_quote(base, i, o, amt, slippage_bps=100, timeout=20.0):
        if not tradeable and i != "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v":
            return None            # sell leg fails => honeypot
        return {"in_amount": float(amt), "out_amount": float(amt) * 0.994,
                "price_impact_pct": 0.6, "route_hops": 1,
                "route_labels": ["Raydium"]}
    jupiter.quote = fake_quote


def _streak_resets(cfg):
    """Open/lose twice, then win: the consecutive-loss counter must clear."""
    import copy
    cfg = copy.deepcopy(cfg)
    cfg.state_dir = tempfile.mkdtemp()      # fresh state, no inherited halt
    pf = Portfolio(cfg, venue.build(cfg))
    for i in range(2):
        pf.open(f"S{i}", "S", 1.0, prices={})
        pf.check_exits({f"S{i}": 0.5})
    before = pf.consecutive_losses
    pf.open("SW", "S", 1.0, prices={})
    pf.check_exits({"SW": 5.0})          # take-profit
    return before == 2 and pf.consecutive_losses == 0


def main():
    print("SOLAI offline smoke test (no network)\n")
    tmp = tempfile.mkdtemp()
    cfg = load_config(state_dir=tmp)
    cfg.mints = ["MintAAA", "MintBBB"]
    cfg.analyst_enabled = False

    # 1. base58 ------------------------------------------------------------
    print("base58")
    check("32 zero bytes -> system program",
          rpc._b58(bytes(32)) == "1" * 32)
    tok = bytes([6, 221, 246, 225, 215, 101, 161, 147, 217, 203, 225, 70, 206,
                 235, 121, 172, 28, 180, 133, 237, 95, 91, 55, 145, 58, 140,
                 245, 133, 126, 255, 0, 169])
    check("known vector -> SPL Token program",
          rpc._b58(tok) == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")

    # 2. mint parsing ------------------------------------------------------
    print("\nmint parsing")
    install_stubs(cfg)
    m = rpc.parse_mint(cfg.rpc_url, "MintAAA")
    check("authorities revoked", m["mint_authority_revoked"] and
          m["freeze_authority_revoked"])
    check("supply/decimals decoded", m["supply"] == 1e9 and m["decimals"] == 6,
          f"supply={m['supply']:,.0f} dec={m['decimals']}")
    install_stubs(cfg, mint_auth=True, freeze=True)
    m2 = rpc.parse_mint(cfg.rpc_url, "MintAAA")
    check("live authorities detected",
          not m2["mint_authority_revoked"] and not m2["freeze_authority_revoked"])

    # 3. holder concentration ---------------------------------------------
    print("\nholder concentration")
    install_stubs(cfg)
    h = rpc.largest_holders(cfg.rpc_url, "MintAAA")
    check("burn address excluded from concentration",
          h["top1_pct"] > 50 and h["top1_pct_ex_burn"] < 10,
          f"raw top1 {h['top1_pct']:.1f}% -> ex-burn {h['top1_pct_ex_burn']:.1f}%")

    # 4. safety screens ----------------------------------------------------
    print("\nsafety screens")
    bundles = gather_many(cfg, ["MintAAA"])
    b = bundles[0]
    b.chain["lp"] = {"safe": True, "lp_supply": 0.0,
                     "lp_mint_authority_revoked": True}
    s = safety.screen(b, cfg.safety)
    check("clean token passes", s["verdict"] == "PASS", s["summary"])

    install_stubs(cfg, mint_auth=True)
    bad = gather_many(cfg, ["MintAAA"])[0]
    bad.chain["lp"] = {"safe": True}
    check("live mint authority is FATAL",
          safety.screen(bad, cfg.safety)["verdict"] == "REJECT",
          safety.screen(bad, cfg.safety)["summary"][:52])

    install_stubs(cfg, tradeable=False)
    hp = gather_many(cfg, ["MintAAA"])[0]
    hp.chain["lp"] = {"safe": True}
    check("honeypot (no sell route) is FATAL",
          safety.screen(hp, cfg.safety)["verdict"] == "REJECT")

    install_stubs(cfg, top1=44.0)
    wh = gather_many(cfg, ["MintAAA"])[0]
    wh.chain["lp"] = {"safe": True}
    check("whale concentration rejected",
          safety.screen(wh, cfg.safety)["verdict"] == "REJECT")

    install_stubs(cfg)
    unk = gather_many(cfg, ["MintAAA"])[0]      # no LP info resolved
    unk_screen = safety.screen(unk, cfg.safety)
    check("unresolved LP passes but carries a loud caveat",
          unk_screen["verdict"] == "PASS" and
          any("LP burn NOT verified" in c for c in unk_screen["caveats"]),
          f"{len(unk_screen['caveats'])} caveat(s) attached")
    strict = SafetyLimits(require_lp_burned=True)
    check("with require_lp_burned ON, unresolved LP blocks the trade",
          safety.screen(unk, strict)["verdict"] == "INCONCLUSIVE")

    # 5. scoring -----------------------------------------------------------
    print("\nscoring")
    sc = scoring.score(b, cfg)
    check("score in range", 0 <= sc["score"] <= 100,
          f"{sc['score']} ({sc['label']}) conf {sc['confidence']}")
    check("hurdle charged for round-trip cost", sc["hurdle_penalty"] > 0,
          f"-{sc['hurdle_penalty']} pts for {sc['round_trip_cost_pct']:.2f}% cost")
    wash = SignalBundle(mint="W")
    wash.micro = {**b.micro, "vol_liq_ratio": 60.0}
    wash.execution = b.execution
    check("wash-trading turnover scores negative",
          next(c["signal"] for c in scoring.score(wash, cfg)["components"]
               if c["name"] == "turnover") < 0)

    # 6. TA warm-up --------------------------------------------------------
    print("\nTA warm-up")
    log = PriceLog(tmp)
    check("cold TA is not scored", (b.ta or {}).get("ready") is False,
          (b.ta or {}).get("reason"))
    for s_ in fx.price_series(400):
        log.append("MintWARM", s_["p"], s_["t"], s_["v"])
    from .signals import _ta
    warm = _ta(log.candles("MintWARM", cfg.ta_interval_minutes, cfg.ta_lookback))
    check("warm TA produces a score", warm["ready"] and warm["score"] is not None,
          f"{warm['candles']} bars -> {warm['score']:+.0f} ({warm['label']})")

    # 7. analyst contract + decisions --------------------------------------
    print("\nanalyst + decisions")
    entry = {"safety": s, "score": sc, "analyst": None}
    check("no analyst => watch, never auto-buy",
          engine.decide({**entry, "analyst": {**fx.ANALYST_OK,
                                              "_error": "anthropic_not_installed",
                                              "verdict": "abstain"}},
                        cfg)["action"] == "watch")
    check("analyst confirm => buy",
          engine.decide({**entry, "analyst": fx.ANALYST_OK}, cfg)["action"] == "buy")
    check("analyst veto => skip",
          engine.decide({**entry, "analyst": fx.ANALYST_VETO}, cfg)["action"] == "skip")
    check("prompt-injection flag => skip",
          engine.decide({**entry, "analyst": {**fx.ANALYST_OK,
                                              "injection_attempt_detected": True}},
                        cfg)["action"] == "skip")
    check("unsafe token never reaches buy",
          engine.decide({"safety": {"verdict": "REJECT", "summary": "x"},
                         "score": sc, "analyst": fx.ANALYST_OK},
                        cfg)["action"] == "skip")

    # 8. paper execution ---------------------------------------------------
    print("\npaper execution")
    cfg.risk.equity_usd = 100.0
    pf = Portfolio(cfg, venue.build(cfg))
    r = pf.open("MintAAA", "AAA", 0.004, score=71.0, verdict="confirm", prices={})
    check("position opened", r["opened"])
    pos = pf.positions["MintAAA"]
    check("size respects cap", pos.entry_usd <= cfg.risk.max_position_usd,
          f"${pos.entry_usd:.2f} <= ${cfg.risk.max_position_usd:.2f}")
    ex = pf.check_exits({"MintAAA": pos.stop_price * 0.99})
    check("stop loss fires", ex and ex[0]["reason"] == "stop_loss",
          f"pnl {ex[0]['pnl_pct']:+.1f}%" if ex else "")
    for i in range(3):
        pf.open(f"L{i}", "L", 1.0, prices={})
        pf.check_exits({f"L{i}": 0.5})
    check("a breaker halts trading after a losing streak",
          pf.halted is not None and pf.can_open("NEW", {})[0] is False,
          f"{pf.halted} after {pf.consecutive_losses} losses")
    # With these defaults the daily-loss breaker binds BEFORE the kill switch:
    # 3 half-losses on 25% positions is already past -20% of day-start equity.
    check("daily-loss breaker binds first at default limits",
          pf.halted == "daily_loss", f"halted={pf.halted}")
    pf.mark({})
    pf2 = Portfolio(cfg, venue.build(cfg))
    check("state survives restart",
          len(pf2.closed) == len(pf.closed) and pf2.halted == pf.halted,
          f"{len(pf2.closed)} trades, halted={pf2.halted}")

    # Kill switch in isolation: give the day plenty of room so consecutive
    # losses are the only thing that can stop trading.
    cfg2 = load_config(state_dir=tempfile.mkdtemp())
    cfg2.risk.equity_usd = 1000.0
    cfg2.risk.max_daily_loss_pct = 99.0
    cfg2.risk.max_position_pct = 2.0
    pf3 = Portfolio(cfg2, venue.build(cfg2))
    for i in range(cfg2.risk.max_consecutive_losses):
        pf3.open(f"K{i}", "K", 1.0, prices={})
        pf3.check_exits({f"K{i}": 0.5})
    check("kill switch halts after N consecutive losses",
          pf3.halted == "kill_switch",
          f"{pf3.consecutive_losses} losses -> {pf3.halted}")
    check("a win resets the loss streak",
          _streak_resets(cfg2), "win between losses clears the counter")
    check("round trip costs money",
          pf.stats({})["total_return_pct"] < 0,
          f"{pf.stats({})['total_return_pct']:+.2f}% after costs")

    # 9. live locks --------------------------------------------------------
    print("\nlive locks")
    cfg.mode = "live"
    cfg.allow_live = False
    try:
        venue.build(cfg)
        check("lock 1: SOLAI_ALLOW_LIVE required", False)
    except PermissionError:
        check("lock 1: SOLAI_ALLOW_LIVE required", True)
    cfg.allow_live = True
    try:
        venue.build(cfg).buy("M", 25.0, 0.004)
        check("lock 2: live venue not implemented", False)
    except NotImplementedError:
        check("lock 2: live venue not implemented", True)

    # 10. full scan --------------------------------------------------------
    print("\nfull pipeline")
    cfg.mode = "scan"
    install_stubs(cfg)
    res = engine.scan(cfg, ["MintAAA", "MintBBB"], run_analyst=False)
    check("scan completes", res["scanned"] == 2,
          f"{res['scanned']} scanned, {res['passed_safety']} passed safety")
    check("every candidate has a decision reason",
          all(c["decision"] and c["decision"].get("reason")
              for c in res["candidates"]))
    check("no analyst calls when disabled", res["analyst_calls"] == 0)

    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}): " + ", ".join(FAILS))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
