"""Risk engine — the guardrails every order must pass.

Tracks realized P&L, open positions, and a consecutive-loss kill switch.
`check_order` returns (ok, reason). The trader MUST call it before any order
(paper or live) and refuse on a False.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0

    @property
    def notional_at(self):
        return abs(self.qty) * self.avg_price


@dataclass
class RiskEngine:
    limits: object                      # RiskLimits
    equity_usd: float = 1000.0          # account equity used for % sizing
    realized_pnl_today: float = 0.0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    positions: dict = field(default_factory=dict)   # symbol -> Position

    # ---- sizing ----

    def position_size_usd(self, entry_price, stop_price=None):
        """Risk-based sizing: risk `risk_per_trade_pct` of equity to the stop."""
        risk_usd = self.equity_usd * self.limits.risk_per_trade_pct / 100.0
        if stop_price and entry_price and stop_price != entry_price:
            stop_dist_frac = abs(entry_price - stop_price) / entry_price
            notional = risk_usd / stop_dist_frac if stop_dist_frac else 0.0
        else:
            # fall back to default stop distance
            notional = risk_usd / (self.limits.default_stop_pct / 100.0)
        return min(notional, self.limits.max_order_usd)

    # ---- gating ----

    def check_order(self, symbol, side, notional_usd):
        if self.halted:
            return False, f"HALTED: {self.halt_reason}"
        if notional_usd > self.limits.max_order_usd + 1e-9:
            return False, (f"order ${notional_usd:.2f} exceeds max_order_usd "
                           f"${self.limits.max_order_usd:.2f}")
        # position cap
        pos = self.positions.get(symbol)
        cur = pos.notional_at if pos else 0.0
        if side == "BUY" and cur + notional_usd > self.limits.max_position_usd + 1e-9:
            return False, (f"position would reach ${cur + notional_usd:.2f} > "
                           f"max_position_usd ${self.limits.max_position_usd:.2f}")
        # open-position count
        open_count = sum(1 for p in self.positions.values() if abs(p.qty) > 0)
        if (side == "BUY" and (not pos or pos.qty == 0)
                and open_count >= self.limits.max_open_positions):
            return False, (f"already at max_open_positions "
                           f"{self.limits.max_open_positions}")
        # daily loss
        if -self.realized_pnl_today >= self.limits.max_daily_loss_usd:
            self.halt(f"daily loss ${-self.realized_pnl_today:.2f} hit limit")
            return False, self.halt_reason
        return True, "ok"

    def halt(self, reason):
        self.halted = True
        self.halt_reason = reason

    # ---- bookkeeping ----

    def on_fill(self, symbol, side, qty, price):
        pos = self.positions.setdefault(symbol, Position(symbol))
        if side == "BUY":
            new_qty = pos.qty + qty
            if new_qty != 0:
                pos.avg_price = (pos.avg_price * pos.qty + price * qty) / new_qty
            pos.qty = new_qty
        else:  # SELL realizes P&L against avg
            realized = (price - pos.avg_price) * min(qty, pos.qty)
            self.realized_pnl_today += realized
            pos.qty -= qty
            if pos.qty <= 1e-12:
                pos.qty = 0.0
                pos.avg_price = 0.0
            self._update_streak(realized)
            return realized
        return 0.0

    def _update_streak(self, realized):
        if realized < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.limits.max_consecutive_losses:
                self.halt(f"kill switch: {self.consecutive_losses} losses in a row")
        elif realized > 0:
            self.consecutive_losses = 0

    def snapshot(self):
        return {
            "equity_usd": round(self.equity_usd, 2),
            "realized_pnl_today": round(self.realized_pnl_today, 2),
            "consecutive_losses": self.consecutive_losses,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "open_positions": {s: {"qty": p.qty, "avg": p.avg_price}
                               for s, p in self.positions.items() if abs(p.qty) > 0},
        }
