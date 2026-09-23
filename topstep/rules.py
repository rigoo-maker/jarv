"""TopStep account rules as an enforceable state machine.

This module is the point of the project. On a prop-firm evaluation the strategy
is not what fails you — the trailing drawdown is. A strategy that makes money
and breaches the Maximum Loss Limit for one hour is worth exactly zero, so the
rules are modelled as a hard gate that sits in front of every order, not as a
report you read afterwards.

Three limits interact, and traders routinely model only the first:

  Max Loss Limit (MLL)  A TRAILING floor. It follows your account's peak up and
                        never comes back down. Being up $2,500 and giving back
                        $2,000 fails the account even though you are net +$500.
  Daily Loss Limit(DLL) A fixed intraday floor, reset each session.
  Consistency           No single day may be more than N% of total profit, or
                        the profit is not payable even though you "passed".

VERIFY THESE NUMBERS BEFORE TRUSTING THEM. TopStep changes account specs,
and whether the MLL trails INTRADAY EQUITY or END-OF-DAY BALANCE is the single
most consequential detail here — it changes position sizing by roughly a
factor of two. Both modes are implemented; `trail_mode` selects. The default
is end-of-day because that is what TopStep has historically used for the
Combine, but confirm it against your account before sizing off it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class AccountSpec:
    """One evaluation tier."""
    name: str
    starting_balance: float
    profit_target: float
    max_loss_limit: float          # trailing
    daily_loss_limit: float
    max_contracts: int             # in E-mini equivalents
    min_trading_days: int = 2
    consistency_pct: float = 50.0  # best day may not exceed this % of profit
    # "eod"      -> MLL trails the highest END-OF-DAY balance (TopStep Combine)
    # "intraday" -> MLL trails the highest INTRADAY equity (stricter; several
    #               other prop firms work this way — check yours)
    trail_mode: str = "eod"
    # The trailing floor stops rising once it reaches this level. On TopStep the
    # MLL locks at the starting balance (i.e. breakeven) and never trails above.
    lock_at_breakeven: bool = True

    @property
    def micro_max_contracts(self) -> int:
        """Micros usually count 1/10th toward the scaling plan."""
        return self.max_contracts * 10


# Published Trading Combine tiers. Re-check against topstep.com before use.
TIERS = {
    "50K":  AccountSpec("50K",  50_000.0, 3_000.0, 2_000.0, 1_000.0, 5),
    "100K": AccountSpec("100K", 100_000.0, 6_000.0, 3_000.0, 2_000.0, 10),
    "150K": AccountSpec("150K", 150_000.0, 9_000.0, 4_500.0, 3_000.0, 15),
}


class Breach(Exception):
    """Raised when an account rule is violated. Terminal — the account is dead."""


@dataclass
class AccountState:
    """Live rule state. Feed it equity marks; ask it whether you may trade."""
    spec: AccountSpec
    balance: float = None                  # closed P&L balance
    peak_balance: float = None             # highest balance seen (per trail_mode)
    day_start_balance: float = None
    open_pnl: float = 0.0
    day: str = None
    daily_results: dict = field(default_factory=dict)   # {day: realized pnl}
    breached: str = None
    passed: bool = False

    def __post_init__(self):
        if self.balance is None:
            self.balance = self.spec.starting_balance
        if self.peak_balance is None:
            self.peak_balance = self.balance
        if self.day_start_balance is None:
            self.day_start_balance = self.balance

    # --- derived limits -----------------------------------------------------
    @property
    def equity(self) -> float:
        return self.balance + self.open_pnl

    @property
    def mll_floor(self) -> float:
        """The equity level that kills the account.

        Trails the peak up by (peak - max_loss_limit), then locks once it
        reaches the starting balance if the spec says so.
        """
        floor = self.peak_balance - self.spec.max_loss_limit
        if self.spec.lock_at_breakeven:
            floor = min(floor, self.spec.starting_balance)
        return floor

    @property
    def dll_floor(self) -> float:
        return self.day_start_balance - self.spec.daily_loss_limit

    @property
    def effective_floor(self) -> float:
        """Whichever limit binds first. This is your real stop-out level."""
        return max(self.mll_floor, self.dll_floor)

    @property
    def room(self) -> float:
        """USD of loss available right now before the account dies.

        This is the only number that should size a position.
        """
        return max(0.0, self.equity - self.effective_floor)

    @property
    def profit(self) -> float:
        return self.balance - self.spec.starting_balance

    @property
    def target_remaining(self) -> float:
        return max(0.0, self.spec.profit_target - self.profit)

    # --- lifecycle ----------------------------------------------------------
    def start_day(self, day: str):
        """Roll to a new session. Call before the first trade of the day."""
        if self.day is not None and day != self.day:
            # End-of-day trailing updates here, on the settled balance only.
            if self.spec.trail_mode == "eod":
                self.peak_balance = max(self.peak_balance, self.balance)
        self.day = day
        self.day_start_balance = self.balance
        self.open_pnl = 0.0
        self.daily_results.setdefault(day, 0.0)

    def mark(self, open_pnl: float = 0.0):
        """Update unrealized P&L and test the floors. Raises Breach on failure."""
        self.open_pnl = open_pnl
        if self.spec.trail_mode == "intraday":
            self.peak_balance = max(self.peak_balance, self.equity)
        self._check()

    def on_trade_closed(self, pnl: float):
        """Realize a trade. pnl is NET (commissions already subtracted)."""
        self.balance += pnl
        self.open_pnl = 0.0
        self.daily_results[self.day] = self.daily_results.get(self.day, 0.0) + pnl
        if self.spec.trail_mode == "intraday":
            self.peak_balance = max(self.peak_balance, self.balance)
        self._check()
        if self.profit >= self.spec.profit_target:
            self.passed = self._eligible()

    def _check(self):
        if self.breached:
            raise Breach(self.breached)
        if self.equity <= self.mll_floor:
            self.breached = (f"MAX LOSS LIMIT: equity ${self.equity:,.2f} <= "
                             f"floor ${self.mll_floor:,.2f} "
                             f"(peak ${self.peak_balance:,.2f})")
            raise Breach(self.breached)
        if self.equity <= self.dll_floor:
            self.breached = (f"DAILY LOSS LIMIT: equity ${self.equity:,.2f} <= "
                             f"floor ${self.dll_floor:,.2f} "
                             f"(day start ${self.day_start_balance:,.2f})")
            raise Breach(self.breached)

    # --- pass conditions ----------------------------------------------------
    def _eligible(self) -> bool:
        return not self.consistency_violation() and \
            len([d for d, v in self.daily_results.items() if v != 0]) >= \
            self.spec.min_trading_days

    def consistency_violation(self):
        """Best profitable day as a share of total profit.

        Returns None when compliant, else a description. One outsized day is
        the most common way a technically-passing account is refused: you hit
        the target but cannot withdraw it.
        """
        if self.profit <= 0:
            return None
        best = max(self.daily_results.values(), default=0.0)
        if best <= 0:
            return None
        share = best / self.profit * 100.0
        if share > self.spec.consistency_pct:
            return (f"best day ${best:,.2f} is {share:.1f}% of ${self.profit:,.2f} "
                    f"profit (cap {self.spec.consistency_pct:.0f}%)")
        return None

    def min_profit_for_consistency(self) -> float:
        """Total profit needed to make the current best day compliant.

        Useful live: it tells you how much more you must grind out on OTHER
        days before your one big day stops blocking the payout.
        """
        best = max(self.daily_results.values(), default=0.0)
        if best <= 0:
            return 0.0
        return best / (self.spec.consistency_pct / 100.0)

    # --- position sizing ----------------------------------------------------
    def max_contracts_for_risk(self, instrument, stop_ticks: int,
                               risk_fraction: float = 0.25) -> int:
        """Largest size whose full stop loss stays inside the risk budget.

        risk_fraction is the share of REMAINING ROOM (not of equity) put at
        risk on one trade. Sizing off equity is the classic error: with a
        $2,000 trailing drawdown on a $50,000 account, 1% of equity is $500 —
        a quarter of the whole account's life on one trade.
        """
        if stop_ticks <= 0:
            return 0
        budget = self.room * risk_fraction
        risk_per_contract = stop_ticks * instrument.tick_value + instrument.commission_rt
        if risk_per_contract <= 0:
            return 0
        n = int(budget // risk_per_contract)
        cap = (instrument.is_micro and self.spec.micro_max_contracts
               or self.spec.max_contracts)
        return max(0, min(n, cap))

    def snapshot(self) -> dict:
        return {
            "tier": self.spec.name,
            "day": self.day,
            "balance": round(self.balance, 2),
            "equity": round(self.equity, 2),
            "peak": round(self.peak_balance, 2),
            "profit": round(self.profit, 2),
            "target_remaining": round(self.target_remaining, 2),
            "mll_floor": round(self.mll_floor, 2),
            "dll_floor": round(self.dll_floor, 2),
            "effective_floor": round(self.effective_floor, 2),
            "room": round(self.room, 2),
            "trading_days": len([d for d, v in self.daily_results.items() if v != 0]),
            "consistency": self.consistency_violation(),
            "breached": self.breached,
            "passed": self.passed,
        }
