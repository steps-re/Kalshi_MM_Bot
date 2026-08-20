"""Cluster-robust means, shared by the offline studies.

Every study in this repo samples many observations from few price paths. A
15-minute window yields dozens of triggers and dozens of fills, all riding the
same path, with overlapping markout windows on top. Treating them as
independent draws is the single error that did the most damage here: it inflated
the taker scan's t-statistics by enough to turn -0.231c at t=-2.5 into -0.980c
at t=-44.4, and it let 931 ticks inside two price paths be published as 931
independent draws.

Cluster on the market ticker. Anything that reports a mean over trades and does
not do this is reporting an error bar it has not earned.
"""

from __future__ import annotations

import math
import statistics as st
from collections import defaultdict


def clustered(values, clusters) -> tuple[int, int, float, float]:
    """Mean and a cluster-robust standard error: (n, groups, mean, se).

    se = sqrt(G/(G-1) * sum_g (sum_{i in g} (x_i - xbar))^2) / n, the usual
    one-way cluster sandwich for a sample mean.
    """

    values = list(values)
    n = len(values)

    if n == 0:
        return 0, 0, float("nan"), float("nan")

    mean = st.mean(values)

    if n == 1:
        return 1, 1, mean, float("nan")

    sums: dict[object, float] = defaultdict(float)

    for value, group in zip(values, clusters, strict=True):
        sums[group] += value - mean

    groups = len(sums)

    if groups < 2:
        return n, groups, mean, float("nan")

    meat = sum(total * total for total in sums.values())
    return n, groups, mean, math.sqrt(groups / (groups - 1) * meat) / n


def clustered_diff(a_values, a_clusters, b_values, b_clusters):
    """mean(a) - mean(b) with a cluster-robust SE, via OLS on an indicator.

    A market that contributes to both arms contributes one combined residual
    sum, so the correlation between its own observations is priced in rather
    than assumed away.
    """

    a_values, b_values = list(a_values), list(b_values)
    values = a_values + b_values
    groups = list(a_clusters) + list(b_clusters)
    flags = [1.0] * len(a_values) + [0.0] * len(b_values)
    n = len(values)

    if not a_values or not b_values or n < 3:
        return float("nan"), float("nan")

    share = sum(flags) / n
    diff = st.mean(a_values) - st.mean(b_values)
    intercept = st.mean(b_values)
    denominator = sum((flag - share) ** 2 for flag in flags)

    if denominator <= 0:
        return diff, float("nan")

    sums: dict[object, float] = defaultdict(float)

    for value, group, flag in zip(values, groups, flags, strict=True):
        sums[group] += (flag - share) * (value - intercept - diff * flag)

    count = len(sums)

    if count < 2:
        return diff, float("nan")

    meat = sum(total * total for total in sums.values())
    return diff, math.sqrt(count / (count - 1) * meat) / denominator


def clustered_pooled(sums, counts) -> tuple[int, int, float, float]:
    """Cluster-robust SE of the POOLED per-observation mean: (n, groups, mean, se).

    `clustered()` takes one value per observation. Callers that have already
    aggregated to cluster level cannot use it: passing a list of cluster MEANS
    puts every value in its own group, so the sandwich silently returns the SE
    of the EQUAL-weighted mean of cluster means. When the reported point
    estimate is the size-weighted pooled mean - as it is for every per-contract
    P&L in this repo - that is an error bar for a different estimator.

    Pass cluster totals instead. `sums[g]` is the sum of the observation values
    in cluster g and `counts[g]` is how many there were, so the residual sum
    sum_{i in g} (x_i - xbar) is recovered exactly as sums[g] - counts[g]*xbar.
    """

    sums, counts = list(sums), list(counts)
    groups = len(sums)
    n = sum(counts)

    if n == 0:
        return 0, 0, float("nan"), float("nan")

    mean = sum(sums) / n

    if groups < 2:
        return n, groups, mean, float("nan")

    meat = sum((total - count * mean) ** 2
               for total, count in zip(sums, counts, strict=True))
    return n, groups, mean, math.sqrt(groups / (groups - 1) * meat) / n


# One-sided 95% Poisson upper limits for an observed count k (Garwood exact),
# converted to a standard deviation as (upper - k)/1.96. At k=0 this is the
# rule of three: 3.0/1.96 = 1.53. sqrt(k) is the asymptotic form and is too
# tight for the small counts these trades actually produce - at k=1 it claims
# sd 1.00 against the exact 1.91.
_POISSON_UPPER = {0: 3.00, 1: 4.74, 2: 6.30, 3: 7.75, 4: 9.15, 5: 10.51,
                  6: 11.84, 7: 13.15, 8: 14.43, 9: 15.71, 10: 16.96,
                  12: 19.44, 15: 23.10, 20: 29.06, 30: 40.69, 50: 63.29}


def poisson_count_sd(k: int) -> float:
    """Standard deviation of a Poisson count, honest at small k."""

    if k in _POISSON_UPPER:
        return max((_POISSON_UPPER[k] - k) / 1.96, math.sqrt(k))

    keys = sorted(_POISSON_UPPER)

    if k > keys[-1]:
        return math.sqrt(k)

    lo = max(key for key in keys if key <= k)
    hi = min(key for key in keys if key >= k)
    weight = (k - lo) / (hi - lo)
    upper = _POISSON_UPPER[lo] + weight * (_POISSON_UPPER[hi] - _POISSON_UPPER[lo])
    return max((upper - k) / 1.96, math.sqrt(k))


def loss_count_floor(losses_per_cluster, n: int) -> float:
    """SE floor from uncertainty in the LOSS COUNT, counted in CLUSTERS.

    Win-small-often / lose-big-rarely trades break the sandwich: with no losses
    in sample every cluster mean is near-identical, the clustered SE collapses
    and t explodes. Tennis printed t=24.6 off ZERO losses in 344 contracts.

    A loss moves total P&L by exactly $1 per contract either way, so the SE of
    the per-contract mean is sd(L)/n and the only question is sd(L). The answer
    is NOT sqrt(L)/n: losses are not independent across contracts. One tennis
    match, one BTC path decides every contract in its cluster together, so
    losses arrive in cluster-sized lumps. Dividing sqrt(L) by the CONTRACT count
    rather than accounting for the lumping understates this floor by roughly
    sqrt(contracts per cluster) - about 3x on this data, the difference between
    a published t=11.0 and t=3.5 on a zero-loss cell.

    Two estimators, take the larger:

    * the empirical cluster-level spread, sqrt(G) * sd(L_g). Assumption-free,
      and it prices clumping directly because a cluster that lost five
      contracts enters as L_g = 5.
    * a Poisson count on the number of loss EVENTS, scaled by how many
      contracts a losing cluster typically takes down. This carries the small-k
      cases where the empirical variance is itself estimated from nothing.

    With zero events observed there is no empirical spread and no observed event
    size, so the rule of three supplies the count (sd 1.53 events) and a typical
    cluster supplies the size (n/G contracts).
    """

    counts = list(losses_per_cluster)
    groups = len(counts)

    if n <= 0 or groups <= 0:
        return 0.0

    events = sum(1 for k in counts if k > 0)
    losses = sum(counts)

    if events == 0:
        return 1.53 * (n / groups) / n

    mean = losses / groups
    spread = math.sqrt(sum((k - mean) ** 2 for k in counts) / (groups - 1)) \
        if groups > 1 else 0.0
    empirical = math.sqrt(groups) * spread
    per_event = max(losses / events, 1.0)
    poisson = poisson_count_sd(events) * per_event
    return max(empirical, poisson) / n


def cluster_t_critical(groups: int) -> float:
    """Two-sided 95% critical value on G-1 df, not the normal's 1.96.

    Cluster-robust inference is asymptotic in the NUMBER OF CLUSTERS, so a cell
    resting on eighteen of them does not get to use 1.96. Small-sample t values,
    interpolated above the table.
    """

    table = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36,
             9: 2.31, 10: 2.26, 12: 2.20, 15: 2.14, 20: 2.09, 25: 2.06,
             30: 2.04, 40: 2.02, 60: 2.00, 120: 1.98}
    df = max(groups - 1, 1)

    if df in table:
        return table[df]

    keys = sorted(table)

    if df < keys[0]:
        return table[keys[0]]

    if df > keys[-1]:
        return 1.96

    lo = max(k for k in keys if k <= df)
    hi = min(k for k in keys if k >= df)
    span = hi - lo
    weight = (df - lo) / span if span else 0.0
    return table[lo] + weight * (table[hi] - table[lo])


def fmt(mean: float, se: float) -> str:
    """A mean is not a result without its error bar, so they print together."""

    if math.isnan(mean):
        return "     n/a"

    if math.isnan(se):
        return f"{mean:+.3f}c    n/a"

    return f"{mean:+.3f}c +/-{se:.3f}"
