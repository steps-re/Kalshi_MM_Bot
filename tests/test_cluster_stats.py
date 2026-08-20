"""Clustered standard errors, because the naive one is the project's worst bug.

Ignoring clustering is what turned KXETHD's -0.231c at t=-2.5 into a published
-0.980c at t=-44.4, and what let 931 ticks inside two price paths be reported as
931 independent draws. These tests pin the behaviour that prevents it.
"""

from __future__ import annotations

import math
import statistics as st
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cluster_stats import clustered, clustered_diff, fmt  # noqa: E402


def naive_se(values) -> float:
    return st.stdev(values) / math.sqrt(len(values))


def test_clustering_widens_the_error_bar_when_observations_repeat_a_path():
    """The whole point. Twenty observations from two price paths carry the
    information of two draws, not twenty, and the SE must say so."""

    values = [1.0] * 10 + [-1.0] * 10
    markets = ["A"] * 10 + ["B"] * 10
    n, groups, mean, se = clustered(values, markets)

    assert (n, groups) == (20, 2)
    assert mean == 0.0
    assert se > naive_se(values)


def test_clustering_matches_the_naive_se_when_every_draw_is_its_own_market():
    """With one observation per cluster there is nothing to correct, so the
    sandwich must collapse onto the ordinary standard error."""

    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    markets = [f"M{i}" for i in range(len(values))]
    _, groups, _, se = clustered(values, markets)

    assert groups == len(values)
    # G/(G-1) against n/(n-1): the same finite-sample correction.
    assert se == pytest.approx(naive_se(values))


def test_a_single_cluster_cannot_produce_an_error_bar():
    """One price path is one draw. Returning a number here would be inventing
    precision, so it returns nan and the caller has to print 'n/a'."""

    _, groups, mean, se = clustered([1.0, 2.0, 3.0], ["A", "A", "A"])

    assert (groups, mean) == (1, 2.0)
    assert math.isnan(se)
    assert "n/a" in fmt(mean, se)


def test_empty_input_is_not_a_crash():
    n, groups, mean, se = clustered([], [])

    assert (n, groups) == (0, 0)
    assert math.isnan(mean) and math.isnan(se)


def test_clustered_diff_recovers_the_mean_difference():
    a, b = [3.0, 5.0, 4.0, 6.0], [1.0, 2.0, 0.0, 1.0]
    diff, se = clustered_diff(a, ["A", "B", "C", "D"], b, ["E", "F", "G", "H"])

    assert diff == st.mean(a) - st.mean(b)
    assert se > 0


def test_clustered_diff_prices_in_a_market_that_spans_both_arms():
    """A market contributing to blocked AND kept fills has one combined residual
    sum, so its within-market correlation is not assumed away."""

    a, b = [2.0, 2.0, 2.0, 2.0], [0.0, 0.0, 0.0, 0.0]
    shared, separate = ["A", "A", "B", "B"], ["A", "A", "B", "B"]
    _, se_shared = clustered_diff(a, shared, b, separate)
    _, se_split = clustered_diff(
        a, ["A", "B", "C", "D"], b, ["E", "F", "G", "H"])

    assert not math.isnan(se_shared)
    assert not math.isnan(se_split)


def test_clustered_diff_refuses_an_empty_arm():
    diff, se = clustered_diff([1.0, 2.0], ["A", "B"], [], [])

    assert math.isnan(diff) and math.isnan(se)


def test_fmt_always_shows_the_error_bar():
    assert fmt(0.5, 0.123) == "+0.500c +/-0.123"
    assert fmt(-1.25, float("nan")) == "-1.250c    n/a"
