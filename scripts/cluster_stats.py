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


def fmt(mean: float, se: float) -> str:
    """A mean is not a result without its error bar, so they print together."""

    if math.isnan(mean):
        return "     n/a"

    if math.isnan(se):
        return f"{mean:+.3f}c    n/a"

    return f"{mean:+.3f}c +/-{se:.3f}"
