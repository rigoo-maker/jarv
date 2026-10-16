"""CME futures contract specs.

Getting these wrong silently corrupts every P&L number downstream, so they are
data, in one place, with the arithmetic derived rather than hardcoded twice.

tick_size   smallest price increment
tick_value  USD per tick per contract
point_value = tick_value / tick_size  (USD per 1.00 price move)
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    tick_size: float
    tick_value: float          # USD per tick, per contract
    is_micro: bool = False
    # Round-turn commission+fees per contract. TopstepX publishes these per
    # product and they change — override from config, do not trust this default.
    commission_rt: float = 1.40
    # Typical session: CME Globex hours in US/Eastern, for time filters.
    rth_open: str = "09:30"
    rth_close: str = "16:00"

    @property
    def point_value(self) -> float:
        return self.tick_value / self.tick_size

    def ticks(self, price_delta: float) -> float:
        return price_delta / self.tick_size

    def pnl(self, entry: float, exit_: float, qty: int, long: bool) -> float:
        """Gross USD P&L, commissions excluded."""
        direction = 1.0 if long else -1.0
        return (exit_ - entry) * direction * self.point_value * qty

    def round_to_tick(self, price: float) -> float:
        return round(price / self.tick_size) * self.tick_size


# Micros are the only sane sizing vehicle for a $50K evaluation: a $2,000
# trailing drawdown cannot absorb ES-sized ticks ($12.50 each).
INSTRUMENTS = {
    # --- equity index ---
    "ES":  Instrument("ES",  "E-mini S&P 500",      0.25, 12.50, commission_rt=4.28),
    "MES": Instrument("MES", "Micro E-mini S&P",    0.25,  1.25, True, 1.34),
    "NQ":  Instrument("NQ",  "E-mini Nasdaq 100",   0.25,  5.00, commission_rt=4.28),
    "MNQ": Instrument("MNQ", "Micro E-mini Nasdaq", 0.25,  0.50, True, 1.34),
    "RTY": Instrument("RTY", "E-mini Russell 2000", 0.10,  5.00, commission_rt=4.28),
    "M2K": Instrument("M2K", "Micro Russell 2000",  0.10,  0.50, True, 1.34),
    "YM":  Instrument("YM",  "E-mini Dow",          1.00,  5.00, commission_rt=4.28),
    "MYM": Instrument("MYM", "Micro Dow",           1.00,  0.50, True, 1.34),
    # --- energy ---
    "CL":  Instrument("CL",  "Crude Oil",           0.01, 10.00, commission_rt=4.28,
                      rth_open="09:00", rth_close="14:30"),
    "MCL": Instrument("MCL", "Micro Crude Oil",     0.01,  1.00, True, 1.34,
                      rth_open="09:00", rth_close="14:30"),
    # --- metals ---
    "GC":  Instrument("GC",  "Gold",                0.10, 10.00, commission_rt=4.28,
                      rth_open="08:20", rth_close="13:30"),
    "MGC": Instrument("MGC", "Micro Gold",          0.10,  1.00, True, 1.34,
                      rth_open="08:20", rth_close="13:30"),
    # --- rates ---
    "ZN":  Instrument("ZN",  "10-Year T-Note",   0.015625, 15.625, commission_rt=3.10,
                      rth_open="08:20", rth_close="15:00"),
}


def get(symbol: str) -> Instrument:
    key = symbol.upper().strip()
    if key in INSTRUMENTS:
        return INSTRUMENTS[key]
    # TopstepX contract ids look like "CON.F.US.MES.Z25" — pull the product out.
    parts = key.split(".")
    for p in parts:
        if p in INSTRUMENTS:
            return INSTRUMENTS[p]
    raise KeyError(f"unknown instrument {symbol!r}; add it to topstep/instruments.py")
