"""Configuration and safety locks for SOLAI.

Same philosophy as krypt.config: every secret and every limit comes from the
environment, nothing is hardcoded, nothing is committed. Live execution is
locked behind two independent switches AND an unimplemented venue, so it
cannot happen by accident.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict

# Well-known mints. WSOL and USDC are the quote assets we price against.
WSOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# SPL Token program IDs. A mint owned by neither is not a normal SPL token.
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return int(default)


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default=None):
    v = os.environ.get(name, "")
    items = [x.strip() for x in v.replace(",", " ").split() if x.strip()]
    return items or list(default or [])


@dataclass
class SafetyLimits:
    """Hard screens. A token failing any of these is rejected before scoring.

    These are deliberately strict. On Solana the dominant loss mode for a small
    account is not a bad entry, it is a token that was never tradeable in the
    first place: mint authority still live, LP not burned, one wallet holding
    most of the supply.
    """
    min_liquidity_usd: float = 25_000.0    # thinner than this and you cannot exit
    min_volume_24h_usd: float = 50_000.0   # no volume means no counterparty
    min_pair_age_minutes: float = 60.0     # sub-hour pairs are a coin flip
    max_pair_age_days: float = 0.0         # 0 = no upper bound
    max_top10_holder_pct: float = 35.0     # excluding known LP/burn accounts
    max_single_holder_pct: float = 12.0
    max_price_impact_pct: float = 3.0      # impact of OUR size, not a fixed lot
    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    # OFF by default, and this is a real limitation rather than a preference.
    # Verifying "LP is burned" needs the pool's LP mint, which means parsing
    # each DEX's pool account layout (Raydium AMM v4, CLMM, Orca Whirlpool,
    # Meteora ... all differ). SOLAI does not do that yet, so leaving this ON
    # would mark every real token INCONCLUSIVE and trade nothing — a screen
    # that blocks everything is not safety, it is a broken scanner.
    # Check LP burn manually before entering, and set this True once you can
    # supply the LP mint (solai.sources.rpc.lp_status verifies it properly).
    require_lp_burned: bool = False

    @classmethod
    def from_env(cls) -> "SafetyLimits":
        return cls(
            min_liquidity_usd=_f("SOLAI_MIN_LIQ_USD", 25_000.0),
            min_volume_24h_usd=_f("SOLAI_MIN_VOL24_USD", 50_000.0),
            min_pair_age_minutes=_f("SOLAI_MIN_PAIR_AGE_MIN", 60.0),
            max_pair_age_days=_f("SOLAI_MAX_PAIR_AGE_DAYS", 0.0),
            max_top10_holder_pct=_f("SOLAI_MAX_TOP10_PCT", 35.0),
            max_single_holder_pct=_f("SOLAI_MAX_SINGLE_HOLDER_PCT", 12.0),
            max_price_impact_pct=_f("SOLAI_MAX_PRICE_IMPACT_PCT", 3.0),
            require_mint_authority_revoked=_b("SOLAI_REQUIRE_MINT_REVOKED", True),
            require_freeze_authority_revoked=_b("SOLAI_REQUIRE_FREEZE_REVOKED", True),
            require_lp_burned=_b("SOLAI_REQUIRE_LP_BURNED", False),
        )


@dataclass
class RiskLimits:
    """Position sizing and circuit breakers, sized for a small account."""
    equity_usd: float = 100.0
    max_position_pct: float = 25.0        # of equity, per token
    max_open_positions: int = 3
    max_daily_loss_pct: float = 20.0      # of starting equity -> halt for the day
    max_consecutive_losses: int = 4       # kill switch
    stop_loss_pct: float = 25.0           # memecoins gap; a 1.5% stop is noise
    take_profit_pct: float = 60.0
    trailing_stop_pct: float = 0.0        # 0 = disabled
    # Round-trip cost assumption. Jupiter route fees + AMM fee + slippage.
    # Deliberately pessimistic: on Solana small caps this is the real number.
    fee_bps: float = 30.0
    slippage_bps: float = 50.0

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            equity_usd=_f("SOLAI_EQUITY_USD", 100.0),
            max_position_pct=_f("SOLAI_MAX_POSITION_PCT", 25.0),
            max_open_positions=_i("SOLAI_MAX_OPEN_POSITIONS", 3),
            max_daily_loss_pct=_f("SOLAI_MAX_DAILY_LOSS_PCT", 20.0),
            max_consecutive_losses=_i("SOLAI_MAX_CONSECUTIVE_LOSSES", 4),
            stop_loss_pct=_f("SOLAI_STOP_PCT", 25.0),
            take_profit_pct=_f("SOLAI_TP_PCT", 60.0),
            trailing_stop_pct=_f("SOLAI_TRAIL_PCT", 0.0),
            fee_bps=_f("SOLAI_FEE_BPS", 30.0),
            slippage_bps=_f("SOLAI_SLIPPAGE_BPS", 50.0),
        )

    @property
    def max_position_usd(self) -> float:
        return self.equity_usd * self.max_position_pct / 100.0

    @property
    def round_trip_cost_pct(self) -> float:
        """What a full in-and-out costs, in percent. Your edge must exceed it."""
        return 2.0 * (self.fee_bps + self.slippage_bps) / 100.0


@dataclass
class Config:
    # --- mode & locks ---
    mode: str = "scan"                    # scan | paper | live
    allow_live: bool = False              # second lock; live is also NotImplemented

    # --- endpoints (all overridable; public defaults are rate-limited) ---
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    dexscreener_url: str = "https://api.dexscreener.com"
    jupiter_url: str = "https://lite-api.jup.ag"

    # --- universe ---
    mints: list = field(default_factory=list)   # explicit watchlist, else discover
    discover_limit: int = 30                    # candidates pulled per scan
    quote_size_usd: float = 25.0                # size used for impact probing

    # --- smart money ---
    smart_wallets: list = field(default_factory=list)
    smart_lookback_sigs: int = 100

    # --- TA ---
    ta_interval_minutes: int = 5
    ta_lookback: int = 200

    # --- analyst ---
    analyst_enabled: bool = True
    analyst_model: str = "claude-opus-5"
    analyst_effort: str = "high"
    analyst_top_n: int = 5                # only the best candidates get an LLM call
    analyst_min_score: float = 55.0       # scorer gate before spending a call

    # --- persistence ---
    state_dir: str = ".solai"

    safety: SafetyLimits = field(default_factory=SafetyLimits)
    risk: RiskLimits = field(default_factory=RiskLimits)

    @property
    def live_unlocked(self) -> bool:
        return self.mode == "live" and self.allow_live

    def to_dict(self):
        d = asdict(self)
        return d


def load_config(**overrides) -> Config:
    cfg = Config(
        mode=os.environ.get("SOLAI_MODE", "scan").strip().lower(),
        allow_live=_b("SOLAI_ALLOW_LIVE", False),
        rpc_url=os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
        dexscreener_url=os.environ.get("SOLAI_DEXSCREENER_URL", "https://api.dexscreener.com"),
        jupiter_url=os.environ.get("SOLAI_JUPITER_URL", "https://lite-api.jup.ag"),
        mints=_list("SOLAI_MINTS"),
        discover_limit=_i("SOLAI_DISCOVER_LIMIT", 30),
        quote_size_usd=_f("SOLAI_QUOTE_SIZE_USD", 25.0),
        smart_wallets=_list("SOLAI_SMART_WALLETS"),
        smart_lookback_sigs=_i("SOLAI_SMART_LOOKBACK", 100),
        ta_interval_minutes=_i("SOLAI_TA_INTERVAL_MIN", 5),
        ta_lookback=_i("SOLAI_TA_LOOKBACK", 200),
        analyst_enabled=_b("SOLAI_ANALYST", True),
        analyst_model=os.environ.get("SOLAI_ANALYST_MODEL", "claude-opus-5"),
        analyst_effort=os.environ.get("SOLAI_ANALYST_EFFORT", "high"),
        analyst_top_n=_i("SOLAI_ANALYST_TOP_N", 5),
        analyst_min_score=_f("SOLAI_ANALYST_MIN_SCORE", 55.0),
        state_dir=os.environ.get("SOLAI_STATE_DIR", ".solai"),
        safety=SafetyLimits.from_env(),
        risk=RiskLimits.from_env(),
    )
    for k, v in overrides.items():
        if v is not None and hasattr(cfg, k):
            setattr(cfg, k, v)
    if cfg.mode not in ("scan", "paper", "live"):
        raise ValueError(f"invalid SOLAI_MODE {cfg.mode!r} (scan|paper|live)")
    return cfg
