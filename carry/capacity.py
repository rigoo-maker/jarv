"""Capacity model for a funding-carry (cash-and-carry) trade.

Why carry needs its own model, when none of the other edges in this repo did:

Every signal in `edgelab` was a FORECAST — its edge, if any, is independent of
how much you trade. Carry is not a forecast. Carry is a RISK TRANSFER PAYMENT:
longs pay shorts because more capital wants leveraged length than wants to
provide it. Your position is the supply of the other side. So your own size is
an argument to your own expected return, and "how do we scale this" has a
closed-form answer rather than a hopeful one.

The model, deliberately simple enough to argue with:

    f(S) = f0 - lambda * S / OI          funding compresses as you supply shorts
    net(S) = f(S) - c                    c = all-in holding + turnover cost
    profit(S) = S * net(S)               dollars, not percent

profit is a downward parabola in S. Differentiating:

    S* = OI * (f0 - c) / (2 * lambda)    the profit-maximising size
    S_dead = OI * (f0 - c) / lambda      the size where the edge is fully gone

So **S\\* is exactly half of S_dead**. That is the whole lesson of scaling
carry: the size that makes the most money is half the size that makes none,
and every dollar past S* earns LESS total profit while carrying MORE risk.
Most carry blowups are not wrong-way bets; they are correct trades run past
S* into a position that cannot be unwound.

lambda is the elasticity: how many basis points of funding you give up per
1% of open interest you add. It is the one parameter that matters and the one
nobody publishes — you must measure it on your own fills. `implied_lambda()`
backs it out of two observations so you can calibrate instead of guess.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Venue:
    """One exchange/instrument pair you can put carry on."""
    name: str
    open_interest_usd: float
    funding_apr: float          # f0: annualised funding, as a decimal (0.12 = 12%)
    # Elasticity: APR given up per unit of (your size / OI). 0.25 means adding
    # short OI equal to 100% of current OI would compress funding by 25 APR pts.
    elasticity: float = 0.25
    # All-in cost of carrying the position for a year, as a decimal APR:
    # both legs' spread amortised over the holding period, taker/maker fees,
    # spot borrow if any, and the rebalance cost of keeping delta flat.
    cost_apr: float = 0.03
    # Haircut for venue risk: probability-weighted loss from exchange failure,
    # socialised losses, withdrawal freezes. NOT optional at scale.
    venue_risk_apr: float = 0.02
    max_fraction_of_oi: float = 0.10   # hard operational cap, not an optimum

    @property
    def hurdle(self) -> float:
        return self.cost_apr + self.venue_risk_apr

    def funding_at(self, size_usd: float) -> float:
        """Funding APR you actually receive once your own size is in the book."""
        if self.open_interest_usd <= 0:
            return 0.0
        return self.funding_apr - self.elasticity * (size_usd / self.open_interest_usd)

    def net_apr_at(self, size_usd: float) -> float:
        return self.funding_at(size_usd) - self.hurdle

    def profit_at(self, size_usd: float) -> float:
        return size_usd * self.net_apr_at(size_usd)

    @property
    def dead_size(self) -> float:
        """Size at which net carry reaches zero. Capacity ceiling, not a target."""
        edge = self.funding_apr - self.hurdle
        if edge <= 0 or self.elasticity <= 0:
            return 0.0
        return self.open_interest_usd * edge / self.elasticity

    @property
    def optimal_size(self) -> float:
        """Profit-maximising size: exactly half of dead_size, then capped."""
        return min(self.dead_size / 2.0,
                   self.open_interest_usd * self.max_fraction_of_oi)

    @property
    def optimal_profit(self) -> float:
        return self.profit_at(self.optimal_size)

    @property
    def binding_constraint(self) -> str:
        if self.dead_size <= 0:
            return "no edge: funding below cost+risk hurdle"
        half = self.dead_size / 2.0
        cap = self.open_interest_usd * self.max_fraction_of_oi
        return "operational OI cap" if cap < half else "funding elasticity"


def implied_lambda(oi_usd, funding_before, funding_after, size_added_usd):
    """Calibrate elasticity from two real observations.

    Measure funding, add a known size, measure funding again. This is the only
    honest way to get lambda; every published carry backtest implicitly assumes
    lambda = 0, which is why they all look scalable and none of them are.
    """
    if size_added_usd <= 0 or oi_usd <= 0:
        return None
    return (funding_before - funding_after) / (size_added_usd / oi_usd)


def allocate(venues, total_capital, steps=400):
    """Greedy marginal allocation across venues.

    Spreads capital by always feeding the next dollar to whichever venue has
    the highest MARGINAL net carry, which is the correct rule when each venue's
    return decays in its own size. This is the real scaling lever: capacity is
    additive across venues in a way it is not within one.
    """
    alloc = {v.name: 0.0 for v in venues}
    chunk = total_capital / steps
    unplaced = 0.0
    for _ in range(steps):
        best, best_marginal = None, 0.0
        for v in venues:
            s = alloc[v.name]
            if s + chunk > v.open_interest_usd * v.max_fraction_of_oi:
                continue
            # Marginal profit of the next chunk, not average yield.
            marginal = (v.profit_at(s + chunk) - v.profit_at(s)) / chunk
            if marginal > best_marginal:
                best, best_marginal = v, marginal
        if best is None:
            unplaced += chunk
            continue
        alloc[best.name] += chunk
    deployed = sum(alloc.values())
    profit = sum(v.profit_at(alloc[v.name]) for v in venues)
    return {"allocation": alloc, "deployed": deployed, "unplaced": unplaced,
            "profit": profit,
            "blended_apr": (profit / deployed) if deployed > 0 else 0.0}


# ---------------------------------------------------------------------------
# The part the yield curve does not show
# ---------------------------------------------------------------------------
def unwind_cost(venue: Venue, size_usd: float, *,
                liquidity_haircut: float = 0.5,
                exit_elasticity_multiple: float = 3.0) -> dict:
    """What it costs to get OUT, in a market where you would want to.

    This is the asymmetry that makes scaled carry dangerous while small carry
    is genuinely market-neutral, and it is absent from every carry backtest:

      * You choose when to enter. You do not choose when to exit. The exit
        happens during a deleveraging, which is exactly when open interest and
        book depth collapse (`liquidity_haircut`).
      * Exiting is more price-impacting than entering. Entering, you are the
        supply everyone wants; exiting, you are competing with every other
        short who is also being forced out (`exit_elasticity_multiple`).
      * Your short perp leg is the squeezed side. Funding can invert violently
        and you pay it while you are trying to leave.

    So the position that earns `net_apr` in calm markets pays a one-off exit
    cost that is quadratic in size. Expressed in YEARS OF CARRY, which is the
    only unit that makes the trade-off legible.
    """
    if venue.open_interest_usd <= 0 or size_usd <= 0:
        return {}
    stressed_oi = venue.open_interest_usd * (1.0 - liquidity_haircut)
    stressed_elasticity = venue.elasticity * exit_elasticity_multiple
    # Impact of liquidating `size` into a book that is `liquidity_haircut`
    # thinner, averaged over the unwind (hence the 1/2).
    impact = 0.5 * stressed_elasticity * (size_usd / stressed_oi)
    cost_usd = impact * size_usd
    annual_carry = venue.profit_at(size_usd)
    return {
        "size_usd": size_usd,
        "size_pct_of_oi": size_usd / venue.open_interest_usd * 100,
        "impact_pct": impact * 100,
        "exit_cost_usd": cost_usd,
        "annual_carry_usd": annual_carry,
        "years_of_carry_lost": (cost_usd / annual_carry) if annual_carry > 0 else float("inf"),
    }


def risk_adjusted_size(venue: Venue, *, max_years_of_carry: float = 1.0, **kw):
    """Largest size whose stressed unwind costs less than N years of carry.

    This is the sizing rule the profit-maximising `optimal_size` does not give
    you. S* maximises expected carry assuming you never have to leave in a
    hurry. This one assumes you will, and asks how much of the accumulated
    edge a single exit is allowed to destroy. It is almost always the smaller
    number, and it is the one to size on.
    """
    lo, hi = 0.0, venue.open_interest_usd * venue.max_fraction_of_oi
    for _ in range(60):
        mid = (lo + hi) / 2
        u = unwind_cost(venue, mid, **kw)
        if not u or u["years_of_carry_lost"] > max_years_of_carry:
            hi = mid
        else:
            lo = mid
    return lo
