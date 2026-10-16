"""Paper portfolio with a persistent, auditable track record.

State survives restarts. Every fill is appended to a JSONL trade log and every
equity mark to an equity log, so after a few weeks of scanning you have an
actual record instead of a screenshot — the thing that separates "I have an
edge" from "I remember winning".

Risk enforcement lives here rather than in the strategy on purpose: a strategy
can be wrong, but the circuit breakers should hold regardless of what the
strategy or the LLM believes.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict


@dataclass
class Position:
    mint: str
    symbol: str
    qty: float
    entry_price: float
    entry_usd: float
    opened_at: int
    stop_price: float = None
    take_profit_price: float = None
    high_water_price: float = None
    score_at_entry: float = None
    analyst_verdict: str = None

    def value_at(self, price):
        return self.qty * price

    def pnl_at(self, price):
        return self.value_at(price) - self.entry_usd

    def pnl_pct_at(self, price):
        return (self.pnl_at(price) / self.entry_usd * 100.0) if self.entry_usd else 0.0


class Portfolio:
    def __init__(self, cfg, venue):
        self.cfg, self.venue = cfg, venue
        self.risk = cfg.risk
        self.dir = cfg.state_dir
        os.makedirs(self.dir, exist_ok=True)
        self.state_path = os.path.join(self.dir, "portfolio.json")
        self.trades_path = os.path.join(self.dir, "trades.jsonl")
        self.equity_path = os.path.join(self.dir, "equity.jsonl")

        self.cash = self.risk.equity_usd
        self.start_equity = self.risk.equity_usd
        self.positions = {}
        self.closed = []
        self.consecutive_losses = 0
        self.day = _today()
        self.day_start_equity = self.risk.equity_usd
        self.halted = None
        self._load()

    # --- persistence -------------------------------------------------------
    def _load(self):
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path) as f:
                s = json.load(f)
        except (json.JSONDecodeError, OSError):
            return                      # corrupt state starts fresh, not crashes
        self.cash = s.get("cash", self.cash)
        self.start_equity = s.get("start_equity", self.start_equity)
        self.positions = {m: Position(**p) for m, p in (s.get("positions") or {}).items()}
        self.closed = s.get("closed", [])
        self.consecutive_losses = s.get("consecutive_losses", 0)
        self.day = s.get("day", self.day)
        self.day_start_equity = s.get("day_start_equity", self.day_start_equity)
        self.halted = s.get("halted")

    def save(self):
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "cash": self.cash,
                "start_equity": self.start_equity,
                "positions": {m: asdict(p) for m, p in self.positions.items()},
                "closed": self.closed[-500:],
                "consecutive_losses": self.consecutive_losses,
                "day": self.day,
                "day_start_equity": self.day_start_equity,
                "halted": self.halted,
            }, f, indent=2)
        os.replace(tmp, self.state_path)     # atomic; a crash cannot truncate it

    def _log(self, path, rec):
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    # --- accounting --------------------------------------------------------
    def equity(self, prices):
        v = self.cash
        for m, p in self.positions.items():
            px = prices.get(m)
            v += p.value_at(px) if px else p.entry_usd
        return v

    def mark(self, prices):
        eq = self.equity(prices)
        self._log(self.equity_path, {"t": int(time.time() * 1000), "equity": eq,
                                     "cash": self.cash,
                                     "open": len(self.positions)})
        return eq

    def _roll_day(self, prices):
        today = _today()
        if today != self.day:
            self.day = today
            self.day_start_equity = self.equity(prices)
            if self.halted == "daily_loss":
                self.halted = None       # a new day clears the daily-loss halt

    # --- risk gates --------------------------------------------------------
    def _halt(self, reason):
        """Set a circuit breaker AND persist it immediately.

        Durability matters here more than anywhere else in this file: a halt
        that lives only in memory is cleared by any restart, which is exactly
        the moment a losing bot would restart.
        """
        if self.halted != reason:
            self.halted = reason
            self.save()
        return reason

    def can_open(self, mint, prices):
        self._roll_day(prices)
        if self.halted:
            return False, f"halted: {self.halted}"
        if mint in self.positions:
            return False, "already holding this token"
        if len(self.positions) >= self.risk.max_open_positions:
            return False, f"at max open positions ({self.risk.max_open_positions})"
        if self.consecutive_losses >= self.risk.max_consecutive_losses:
            self._halt("kill_switch")
            return False, (f"kill switch: {self.consecutive_losses} consecutive "
                           f"losses")
        eq = self.equity(prices)
        dd = (self.day_start_equity - eq) / self.day_start_equity * 100.0 \
            if self.day_start_equity else 0.0
        if dd >= self.risk.max_daily_loss_pct:
            self._halt("daily_loss")
            return False, f"daily loss {dd:.1f}% >= {self.risk.max_daily_loss_pct}%"
        size = self.position_size(eq)
        if size <= 0 or size > self.cash:
            return False, f"insufficient cash (${self.cash:.2f} for ${size:.2f})"
        return True, None

    def position_size(self, equity=None):
        eq = equity if equity is not None else self.risk.equity_usd
        return min(eq * self.risk.max_position_pct / 100.0, self.cash)

    # --- trading -----------------------------------------------------------
    def open(self, mint, symbol, price, *, score=None, verdict=None, prices=None):
        prices = prices or {}
        ok, why = self.can_open(mint, prices)
        if not ok:
            return {"opened": False, "reason": why}
        size = self.position_size(self.equity(prices))
        fill = self.venue.buy(mint, size, price)
        pos = Position(
            mint=mint, symbol=symbol, qty=fill["qty"],
            entry_price=fill["price"], entry_usd=size,
            opened_at=int(time.time() * 1000),
            stop_price=fill["price"] * (1 - self.risk.stop_loss_pct / 100.0),
            take_profit_price=fill["price"] * (1 + self.risk.take_profit_pct / 100.0),
            high_water_price=fill["price"],
            score_at_entry=score, analyst_verdict=verdict,
        )
        self.cash -= size
        self.positions[mint] = pos
        rec = {"t": pos.opened_at, "event": "open", **fill,
               "symbol": symbol, "score": score, "verdict": verdict,
               "stop": pos.stop_price, "tp": pos.take_profit_price}
        self._log(self.trades_path, rec)
        self.save()
        return {"opened": True, "position": pos, "fill": fill}

    def close(self, mint, price, reason="manual"):
        pos = self.positions.get(mint)
        if not pos:
            return {"closed": False, "reason": "no position"}
        fill = self.venue.sell(mint, pos.qty, price)
        pnl = fill["usd"] - pos.entry_usd
        pnl_pct = pnl / pos.entry_usd * 100.0 if pos.entry_usd else 0.0
        self.cash += fill["usd"]
        del self.positions[mint]

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if self.consecutive_losses >= self.risk.max_consecutive_losses:
            self.halted = "kill_switch"     # persisted by the save() below

        rec = {"t": int(time.time() * 1000), "event": "close", **fill,
               "symbol": pos.symbol, "reason": reason, "pnl": pnl,
               "pnl_pct": pnl_pct, "held_ms": int(time.time() * 1000) - pos.opened_at,
               "score_at_entry": pos.score_at_entry,
               "analyst_verdict": pos.analyst_verdict}
        self.closed.append(rec)
        self._log(self.trades_path, rec)
        self.save()
        return {"closed": True, "reason": reason, "pnl": pnl,
                "pnl_pct": pnl_pct, "fill": fill}

    def check_exits(self, prices):
        """Stops, take-profits, and the trailing stop. Runs before any new entry."""
        out = []
        for mint, pos in list(self.positions.items()):
            px = prices.get(mint)
            if not px:
                continue
            if pos.high_water_price is None or px > pos.high_water_price:
                pos.high_water_price = px
            trail = self.risk.trailing_stop_pct
            if trail > 0 and pos.high_water_price:
                trail_stop = pos.high_water_price * (1 - trail / 100.0)
                if px <= trail_stop and px > pos.stop_price:
                    out.append(self.close(mint, px, "trailing_stop") |
                               {"mint": mint, "symbol": pos.symbol})
                    continue
            if px <= pos.stop_price:
                out.append(self.close(mint, px, "stop_loss") |
                           {"mint": mint, "symbol": pos.symbol})
            elif px >= pos.take_profit_price:
                out.append(self.close(mint, px, "take_profit") |
                           {"mint": mint, "symbol": pos.symbol})
        if out:
            self.save()
        return out

    # --- track record ------------------------------------------------------
    def stats(self, prices=None):
        prices = prices or {}
        eq = self.equity(prices)
        wins = [c for c in self.closed if c["pnl"] > 0]
        losses = [c for c in self.closed if c["pnl"] <= 0]
        gross_win = sum(c["pnl"] for c in wins)
        gross_loss = abs(sum(c["pnl"] for c in losses))
        rets = [c["pnl_pct"] / 100.0 for c in self.closed]

        return {
            "equity": round(eq, 2),
            "cash": round(self.cash, 2),
            "open_positions": len(self.positions),
            "start_equity": self.start_equity,
            "total_return_pct": round((eq / self.start_equity - 1) * 100, 2)
                                if self.start_equity else 0.0,
            "trades": len(self.closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / len(self.closed) * 100, 1)
                            if self.closed else None,
            "avg_win_pct": round(sum(c["pnl_pct"] for c in wins) / len(wins), 2)
                           if wins else None,
            "avg_loss_pct": round(sum(c["pnl_pct"] for c in losses) / len(losses), 2)
                            if losses else None,
            "profit_factor": round(gross_win / gross_loss, 2)
                             if gross_loss > 0 else (None if not wins else float("inf")),
            "expectancy_pct": round(sum(rets) / len(rets) * 100, 3) if rets else None,
            "sharpe": _sharpe(rets),
            "max_drawdown_pct": self._max_dd(),
            "consecutive_losses": self.consecutive_losses,
            "halted": self.halted,
        }

    def _max_dd(self):
        """Peak-to-trough on the logged equity curve."""
        curve = []
        if os.path.exists(self.equity_path):
            with open(self.equity_path) as f:
                for line in f:
                    try:
                        curve.append(json.loads(line)["equity"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        if len(curve) < 2:
            return None
        peak, dd = curve[0], 0.0
        for v in curve:
            peak = max(peak, v)
            if peak > 0:
                dd = max(dd, (peak - v) / peak * 100.0)
        return round(dd, 2)


def _sharpe(rets):
    """Per-trade Sharpe. Not annualized — annualizing a handful of memecoin
    trades produces a number that is impressive and meaningless."""
    n = len(rets)
    if n < 2:
        return None
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    sd = math.sqrt(var)
    return round(mean / sd, 3) if sd > 0 else None


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())
