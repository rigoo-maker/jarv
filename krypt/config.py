"""Configuration & safety locks for KRYPT.

All secrets and limits come from environment variables — nothing is hardcoded
and nothing is committed. Copy `.env.example` to `.env`, fill it in, and
`source` it (or use a dotenv loader) before running.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict


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


@dataclass
class RiskLimits:
    """Hard pre-trade limits. An order violating any of these is rejected."""
    max_order_usd: float = 50.0          # biggest single order notional
    max_position_usd: float = 200.0      # biggest position per symbol
    max_open_positions: int = 3          # concurrent positions allowed
    max_daily_loss_usd: float = 100.0    # cumulative realized loss -> halt
    max_consecutive_losses: int = 4      # kill switch after N losers in a row
    risk_per_trade_pct: float = 1.0      # % of equity risked per trade (sizing)
    default_stop_pct: float = 1.5        # stop-loss distance, % from entry
    default_take_profit_pct: float = 2.0 # take-profit distance, % from entry

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            max_order_usd=_f("KRYPT_MAX_ORDER_USD", 50.0),
            max_position_usd=_f("KRYPT_MAX_POSITION_USD", 200.0),
            max_open_positions=_i("KRYPT_MAX_OPEN_POSITIONS", 3),
            max_daily_loss_usd=_f("KRYPT_MAX_DAILY_LOSS_USD", 100.0),
            max_consecutive_losses=_i("KRYPT_MAX_CONSECUTIVE_LOSSES", 4),
            risk_per_trade_pct=_f("KRYPT_RISK_PER_TRADE_PCT", 1.0),
            default_stop_pct=_f("KRYPT_STOP_PCT", 1.5),
            default_take_profit_pct=_f("KRYPT_TP_PCT", 2.0),
        )


@dataclass
class Config:
    # --- mode & safety ---
    mode: str = "analyze"                # analyze | paper | live
    allow_live: bool = False             # second lock for live (env KRYPT_ALLOW_LIVE)
    testnet: bool = True                 # live orders go to Binance testnet by default

    # --- execution venue ---
    venue: str = "binance"               # binance | coinbase (where orders go)

    # --- Binance credentials ---
    api_key: str = ""
    api_secret: str = ""

    # --- Coinbase Advanced Trade credentials (JWT/ES256) ---
    cb_key_name: str = ""                # organizations/<org>/apiKeys/<uuid>
    cb_private_key: str = ""             # EC PRIVATE KEY PEM (\n allowed)

    # --- universe & cadence ---
    symbols: list = field(default_factory=lambda: ["BTCUSDT"])
    interval: str = "1m"                 # primary kline interval
    refresh_secs: float = 5.0            # dashboard / scan loop cadence

    # --- strategy selection ---
    strategy: str = "scalper"            # scalper | market_maker | hedge | trend

    risk: RiskLimits = field(default_factory=RiskLimits)

    # --- endpoints (resolved from testnet flag) ---
    @property
    def spot_base(self) -> str:
        return ("https://testnet.binance.vision"
                if self.testnet else "https://api.binance.com")

    @property
    def futures_base(self) -> str:
        return ("https://testnet.binancefuture.com"
                if self.testnet else "https://fapi.binance.com")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    def assert_live_allowed(self) -> None:
        """Two-lock guard. Raises unless BOTH locks are satisfied."""
        if self.mode != "live":
            return
        if not self.allow_live:
            raise PermissionError(
                "LIVE mode requires the second lock. Set env KRYPT_ALLOW_LIVE=1 "
                "to confirm you intend to place REAL orders. Refusing to trade.")
        if self.venue == "coinbase":
            if not (self.cb_key_name and self.cb_private_key):
                raise PermissionError(
                    "LIVE mode on Coinbase needs COINBASE_API_KEY_NAME and "
                    "COINBASE_API_PRIVATE_KEY in the environment. None found.")
        elif not (self.api_key and self.api_secret):
            raise PermissionError(
                "LIVE mode on Binance needs BINANCE_API_KEY and BINANCE_API_SECRET "
                "in the environment. None found.")

    def banner(self) -> str:
        if self.venue == "coinbase":
            # Coinbase Advanced Trade has no fake-money testnet for KRYPT's path;
            # use mode=paper to simulate. Live on Coinbase = REAL money, always.
            net = "Coinbase LIVE (REAL money)" if self.mode == "live" else "Coinbase"
        else:
            net = "TESTNET (fake money)" if self.testnet else "MAINNET (REAL money)"
        warn = ""
        if self.mode == "live" and (self.venue == "coinbase" or not self.testnet):
            warn = "  <<< REAL FUNDS AT RISK >>>"
        return (f"mode={self.mode.upper()}  venue={self.venue}  net={net}  "
                f"strategy={self.strategy}  symbols={','.join(self.symbols)}  "
                f"interval={self.interval}{warn}")

    def safe_dict(self) -> dict:
        d = asdict(self)
        d.pop("api_key", None)
        d.pop("api_secret", None)
        d["spot_base"] = self.spot_base
        return d


def load_config(**overrides) -> Config:
    """Build Config from environment, then apply explicit overrides (CLI)."""
    cfg = Config(
        mode=os.environ.get("KRYPT_MODE", "analyze").lower(),
        allow_live=_b("KRYPT_ALLOW_LIVE", False),
        testnet=_b("BINANCE_TESTNET", True),
        venue=os.environ.get("KRYPT_VENUE", "binance").lower(),
        api_key=os.environ.get("BINANCE_API_KEY", ""),
        api_secret=os.environ.get("BINANCE_API_SECRET", ""),
        cb_key_name=os.environ.get("COINBASE_API_KEY_NAME", ""),
        cb_private_key=os.environ.get("COINBASE_API_PRIVATE_KEY", ""),
        symbols=[s.strip().upper() for s in
                 os.environ.get("KRYPT_SYMBOLS", "BTCUSDT").split(",") if s.strip()],
        interval=os.environ.get("KRYPT_INTERVAL", "1m"),
        refresh_secs=_f("KRYPT_REFRESH_SECS", 5.0),
        strategy=os.environ.get("KRYPT_STRATEGY", "scalper").lower(),
        risk=RiskLimits.from_env(),
    )
    for k, v in overrides.items():
        if v is None:
            continue
        if k == "symbols" and isinstance(v, str):
            v = [s.strip().upper() for s in v.split(",") if s.strip()]
        setattr(cfg, k, v)
    return cfg
