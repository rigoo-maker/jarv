"""Performance statistics that do not flatter the strategy."""

from __future__ import annotations

import math
from datetime import date


def _d(s):
    y, m, dd = map(int, s.split("-"))
    return date(y, m, dd)


def perf(dates, rets, *, leverage=1.0, financing_rate=0.0):
    """Annualised performance from a dated return series.

    `dates` and `rets` are parallel; a zero return means a flat (uninvested)
    period, which still counts as elapsed time.

    Annualisation comes from CALENDAR SPAN, not from len(rets). This is the
    single most common error in retail backtests: a strategy that trades 44
    times a year, annualised as if its 44 observations were 252 daily ones,
    reports a Sharpe inflated by sqrt(252/44) = 2.4x.

    `financing_rate` is charged on borrowed exposure only (leverage above 1),
    which is what makes a levered comparison honest.
    """
    n = len(rets)
    if n < 30:
        return None
    years = (_d(dates[-1]) - _d(dates[0])).days / 365.25
    if years <= 0:
        return None
    obs_per_year = n / years
    daily_financing = max(0.0, leverage - 1.0) * financing_rate / obs_per_year
    rr = [r * leverage - daily_financing for r in rets]

    mean = sum(rr) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rr) / (n - 1)) if n > 1 else 0.0

    equity, peak, max_dd = 1.0, 1.0, 0.0
    for r in rr:
        equity *= 1 + r
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    return {
        "n": n,
        "years": round(years, 2),
        "obs_per_year": round(obs_per_year, 1),
        "cagr_pct": (equity ** (1 / years) - 1) * 100,
        "vol_pct": sd * math.sqrt(obs_per_year) * 100,
        "sharpe": (mean / sd * math.sqrt(obs_per_year)) if sd else 0.0,
        "t_stat": (mean / (sd / math.sqrt(n))) if sd else 0.0,
        "max_dd_pct": max_dd * 100,
        "final_equity": equity,
    }


def welch_t(sample, baseline):
    """Is the conditional mean different from the unconditional one?

    The number that actually matters for a signal. A strategy can show a
    strongly positive mean return and still carry no information, because the
    asset itself drifts up. What makes a signal a signal is the INCREMENT over
    simply being invested, and that increment needs its own significance test.
    """
    na, nb = len(sample), len(baseline)
    if na < 2 or nb < 2:
        return None
    ma, mb = sum(sample) / na, sum(baseline) / nb
    va = sum((x - ma) ** 2 for x in sample) / (na - 1)
    vb = sum((x - mb) ** 2 for x in baseline) / (nb - 1)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return None
    return {"edge": ma - mb, "edge_bp": (ma - mb) * 1e4,
            "t_stat": (ma - mb) / se, "n_sample": na, "n_baseline": nb}


def bonferroni_t(n_tests, alpha=0.05):
    """Approximate two-sided t threshold after correcting for n_tests.

    Crude (normal approximation) but the right order of magnitude, and far
    better than the usual practice of quoting t>2 after trying fifty things.
    Searching 70 strategies and reporting the best at t=2.7 is reporting noise.
    """
    if n_tests < 1:
        n_tests = 1
    p = alpha / n_tests
    # Inverse normal via Beasley-Springer-Moro-ish rational approximation.
    q = 1 - p / 2
    t = math.sqrt(-2 * math.log(1 - q)) if q > 0.5 else 0.0
    # Newton refinement against the normal CDF.
    for _ in range(60):
        cdf = 0.5 * (1 + math.erf(t / math.sqrt(2)))
        pdf = math.exp(-t * t / 2) / math.sqrt(2 * math.pi)
        if pdf == 0:
            break
        t -= (cdf - q) / pdf
    return abs(t)


def deflated_sharpe(observed_sharpe, n_tests, n_obs):
    """Haircut a Sharpe for the fact that it is the max of many trials.

    The expected maximum Sharpe from n_tests independent NOISE strategies is
    well above zero, so a Sharpe only counts as evidence insofar as it exceeds
    that. This is a simplified Bailey/Lopez de Prado deflation: good enough to
    show whether a result is distinguishable from the best of N coin flips.
    """
    if n_tests <= 1 or n_obs < 2:
        return observed_sharpe
    euler = 0.5772156649
    # Expected max of n_tests standard normals.
    e_max = ((1 - euler) * _inv_norm(1 - 1.0 / n_tests)
             + euler * _inv_norm(1 - 1.0 / (n_tests * math.e)))
    se_sharpe = math.sqrt(1.0 / n_obs)
    return observed_sharpe - e_max * se_sharpe


def _inv_norm(p):
    p = min(max(p, 1e-12), 1 - 1e-12)
    t = math.sqrt(-2 * math.log(min(p, 1 - p)))
    for _ in range(60):
        cdf = 0.5 * (1 + math.erf(t / math.sqrt(2)))
        pdf = math.exp(-t * t / 2) / math.sqrt(2 * math.pi)
        if pdf == 0:
            break
        t -= (cdf - p) / pdf
    return t
