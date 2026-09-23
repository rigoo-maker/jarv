"""Risk governor — sits between a strategy and the order API.

Every order passes through `approve()`. Nothing reaches the gateway that could
put the account below its effective floor, and no entry is accepted without a
protective stop. This is the inverse of the usual arrangement, where risk is a
report you read after the damage.

Live trading is behind two locks AND is not implemented:
  TOPSTEP_MODE=live  and  TOPSTEP_ALLOW_LIVE=1
Even then `LiveBroker` raises NotImplementedError. That is deliberate. Sending
real futures orders from an unattended loop against a trailing-drawdown account
is how a $50K evaluation dies in one afternoon; prove the strategy in paper
mode against the same governor first.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from .rules import AccountState, Breach


@dataclass
class Decision:
    approved: bool
    qty: int = 0
    reason: str = ""
    detail: dict = field(default_factory=dict)


class RiskGovernor:
    """Pre-trade gate. Reject-by-default."""

    def __init__(self, state: AccountState, instrument, *,
                 risk_fraction=0.25, max_trades_per_day=5,
                 flatten_minute=955, stop_trading_at_target=True,
                 min_room=200.0):
        self.state = state
        self.instrument = instrument
        self.risk_fraction = risk_fraction
        self.max_trades_per_day = max_trades_per_day
        self.flatten_minute = flatten_minute
        self.stop_trading_at_target = stop_trading_at_target
        # Refuse to trade on fumes: below this much room, one normal loss is
        # terminal, so the correct size is zero, not "one micro and hope".
        self.min_room = min_room
        self.trades_today = 0
        self.day = None

    def start_day(self, day):
        self.state.start_day(day)
        if day != self.day:
            self.trades_today = 0
        self.day = day

    def approve(self, signal, minute_of_day=None) -> Decision:
        s, st = signal, self.state

        if st.breached:
            return Decision(False, reason=f"account breached: {st.breached}")
        if self.stop_trading_at_target and st.passed:
            return Decision(False, reason="profit target reached — stop trading, "
                                          "do not give it back")
        if st.target_remaining <= 0 and self.stop_trading_at_target:
            return Decision(False, reason="target already met")
        if self.trades_today >= self.max_trades_per_day:
            return Decision(False, reason=f"daily trade cap "
                                          f"({self.max_trades_per_day}) reached")
        if minute_of_day is not None and minute_of_day >= self.flatten_minute:
            return Decision(False, reason="past flatten time — no new entries")
        if s is None or s.stop_distance <= 0:
            return Decision(False, reason="signal has no protective stop; "
                                          "unsizeable and therefore untradeable")
        if st.room < self.min_room:
            return Decision(False, reason=f"only ${st.room:,.2f} of room left "
                                          f"(floor ${st.effective_floor:,.2f}); "
                                          f"correct size is zero")

        ticks = s.stop_ticks(self.instrument)
        qty = st.max_contracts_for_risk(self.instrument, ticks,
                                        self.risk_fraction)
        if qty <= 0:
            return Decision(False, reason=f"stop is {ticks} ticks "
                                          f"(${ticks * self.instrument.tick_value:,.2f}"
                                          f"/contract) — too wide for "
                                          f"${st.room:,.2f} of room")

        worst = ticks * self.instrument.tick_value * qty + \
            self.instrument.commission_rt * qty
        if worst >= st.room:
            return Decision(False, reason=f"worst case ${worst:,.2f} >= room "
                                          f"${st.room:,.2f}")

        # A consistency check that is genuinely actionable: if today is already
        # carrying too much of the profit, a further win makes the payout worse.
        cons = None
        if st.profit > 0:
            today = st.daily_results.get(st.day, 0.0)
            projected_best = max(max(st.daily_results.values(), default=0.0),
                                 today + worst)
            if st.profit > 0 and projected_best / st.profit * 100 > \
                    st.spec.consistency_pct:
                cons = ("today is on track to exceed the consistency cap — "
                        "profit may not be withdrawable")

        return Decision(True, qty=qty,
                        reason=f"{qty}x {self.instrument.symbol}, stop {ticks} "
                               f"ticks, risk ${worst:,.2f} of ${st.room:,.2f} room",
                        detail={"stop_ticks": ticks, "worst_case": round(worst, 2),
                                "room": round(st.room, 2),
                                "room_used_pct": round(worst / st.room * 100, 1),
                                "consistency_warning": cons})

    def on_fill(self):
        self.trades_today += 1

    def on_trade_closed(self, net_pnl):
        self.state.on_trade_closed(net_pnl)

    def must_flatten(self, minute_of_day) -> bool:
        return minute_of_day >= self.flatten_minute


class PaperBroker:
    """Simulated fills against observed prices, with costs on both sides."""
    name = "paper"

    def __init__(self, instrument, slippage_ticks=1):
        self.instrument = instrument
        self.slippage_ticks = slippage_ticks
        self.position = None

    def enter(self, side, qty, price):
        slip = self.slippage_ticks * self.instrument.tick_size
        fill = price + slip if side == "long" else price - slip
        self.position = {"side": side, "qty": qty, "entry": fill,
                         "at": time.time()}
        return dict(self.position)

    def exit(self, price, reason="manual"):
        if not self.position:
            return None
        p = self.position
        slip = self.slippage_ticks * self.instrument.tick_size
        fill = price - slip if p["side"] == "long" else price + slip
        gross = self.instrument.pnl(p["entry"], fill, p["qty"], p["side"] == "long")
        comm = self.instrument.commission_rt * p["qty"]
        self.position = None
        return {"gross": gross, "commission": comm, "net": gross - comm,
                "exit": fill, "reason": reason, **p}

    def unrealized(self, price):
        if not self.position:
            return 0.0
        p = self.position
        return self.instrument.pnl(p["entry"], price, p["qty"],
                                   p["side"] == "long") - \
            self.instrument.commission_rt * p["qty"]


class LiveBroker:
    """Real orders via TopstepClient. NOT IMPLEMENTED BY DESIGN.

    To implement you would need, at minimum:
      1. Place the entry, then IMMEDIATELY place the protective stop as a
         linked/OCO order — never leave an unprotected position, not even for
         one round trip of latency.
      2. Reconcile against /api/Position/searchOpen every cycle: the gateway,
         not your local variable, is the truth about what you hold.
      3. Handle partial fills — size 4 filling 2 changes your risk arithmetic.
      4. Handle rejects and disconnects: on a dropped SignalR feed, flatten,
         do not guess.
      5. Re-read the account's true trailing drawdown from the platform rather
         than your local model; if the two disagree, believe the platform.
    """
    name = "live"

    def __init__(self, client, account_id, contract_id, instrument):
        self.client, self.account_id = client, account_id
        self.contract_id, self.instrument = contract_id, instrument

    def _refuse(self):
        raise NotImplementedError(
            "Live order routing is intentionally not implemented. Run "
            "TOPSTEP_MODE=paper against the same RiskGovernor until the record "
            "justifies real money, then implement LiveBroker deliberately and "
            "review it. See the docstring in topstep/live.py for the five "
            "things it must handle.")

    def enter(self, *a, **k):
        self._refuse()

    def exit(self, *a, **k):
        self._refuse()


def build_broker(cfg_mode, instrument, client=None, account_id=None,
                 contract_id=None, slippage_ticks=1):
    if cfg_mode == "live":
        if os.environ.get("TOPSTEP_ALLOW_LIVE", "0").strip() not in ("1", "true", "yes"):
            raise PermissionError(
                "TOPSTEP_MODE=live requires the second lock: TOPSTEP_ALLOW_LIVE=1. "
                "(Even then live routing is not implemented — see live.py.)")
        return LiveBroker(client, account_id, contract_id, instrument)
    return PaperBroker(instrument, slippage_ticks=slippage_ticks)
