"""Edge lab — a harness for testing whether a trading edge is real.

Built because the previous two packages in this repo (krypt, solai, topstep)
could all backtest a strategy but none could answer the only question that
matters: is the apparent edge real, or an artifact of having looked at many
strategies until one looked good?

The three mistakes this harness exists to prevent, all of which it caught in
its own first run on real data:

  1. Annualising per-TRADE returns as if they were daily. A strategy trading
     44 times a year scaled by sqrt(252) reports a Sharpe 2.4x too high.
     `perf()` derives the scaling from calendar time, never from bar count.
  2. Reporting the best of N tests without paying for N. Seventy tests gives
     you a ~97% chance of at least one t>2 on pure noise.
  3. Judging a strategy on unlevered Sharpe. A high-Sharpe, low-volatility
     strategy must be levered to compete, and the financing cost of that
     leverage is usually what kills it. `acid_test()` charges it.
"""
__version__ = "0.1.0"
