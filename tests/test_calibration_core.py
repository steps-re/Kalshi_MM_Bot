"""The calibration trades, and the error bars they are allowed to claim.

Each test here pins a defect that shipped. The comments say which one, because
a test whose motivation is lost gets deleted the next time it is inconvenient.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibration_core import (MAX_STALENESS, book_at, cluster_key,  # noqa: E402
                              decay_panel, load_records, trade_pnl, zone_of)
from cluster_stats import (cluster_t_critical, clustered,  # noqa: E402
                           clustered_pooled, loss_count_floor,
                           poisson_count_sd)


def candle(ts: int, bid: str, ask: str) -> dict:
    return {"end_period_ts": ts,
            "yes_bid": {"close_dollars": bid},
            "yes_ask": {"close_dollars": ask},
            "price": {"mean_dollars": bid},
            "volume_fp": "10"}


# --------------------------------------------------------------------------
# book_at: the price must be a price from ROUGHLY THEN
# --------------------------------------------------------------------------

def test_a_stale_book_is_not_the_book_at_t_minus_x():
    """The defect: `book_at` took the last candle at or before T-minus-X with no
    age limit, so a market that stopped printing an hour earlier handed back
    that hour-old book as "the price two minutes before close". A quarter of all
    book reads in the study were stale by more than three minutes, and 6% by
    more than thirty."""

    close = 100_000
    old = [candle(close - 3600, "0.30", "0.34")]

    assert book_at(old, close - 120) is None
    assert book_at(old, close - 120, max_staleness=float("inf")) == (0.30, 0.34)


def test_a_fresh_book_is_taken():
    close = 100_000
    candles = [candle(close - 3600, "0.10", "0.14"),
               candle(close - 130, "0.30", "0.34")]

    assert book_at(candles, close - 120) == (0.30, 0.34)


def test_the_staleness_limit_is_measured_from_the_asked_moment():
    close = 100_000
    candles = [candle(close - 120 - MAX_STALENESS - 1, "0.30", "0.34")]

    assert book_at(candles, close - 120) is None

    candles = [candle(close - 120 - MAX_STALENESS + 1, "0.30", "0.34")]

    assert book_at(candles, close - 120) == (0.30, 0.34)


@pytest.mark.parametrize("bid,ask", [
    ("0.001", "0.50"),      # unopened placeholder bid
    ("0.50", "1.0000"),     # unopened placeholder ask
    ("0.60", "0.55"),       # crossed
    ("0.30", "0.45"),       # wider than MAX_SPREAD
])
def test_an_untradeable_book_is_not_a_price(bid, ask):
    close = 100_000

    assert book_at([candle(close - 130, bid, ask)], close - 120) is None


# --------------------------------------------------------------------------
# zone_of: one definition, and it used to be two
# --------------------------------------------------------------------------

def test_a_mid_of_exactly_five_cents_is_a_tail():
    """The defect: `calibration_at_t` selected tails by BUCKET edge
    (`hi <= 0.05`), which drops a mid of exactly 5c, while the website builder
    used `mid <= 0.05`, which keeps it. Two populations, one "pre-specified"
    trade, and the more inclusive one is what shipped to the page."""

    assert zone_of(0.05) == "tail SELL"
    assert zone_of(0.0501) is None
    assert zone_of(0.80) == "fave BUY"
    assert zone_of(0.7999) is None


# --------------------------------------------------------------------------
# trade_pnl: what a loss actually costs, which the SE floor rests on
# --------------------------------------------------------------------------

@pytest.mark.parametrize("zone,bid,ask", [
    ("tail SELL", 0.03, 0.05),
    ("fave BUY", 0.84, 0.86),
])
def test_a_loss_moves_pnl_by_exactly_one_dollar(zone, bid, ask):
    """The floor converts loss-COUNT uncertainty into dollars, which is only
    valid because one extra loss costs exactly $1 per contract whatever the
    entry price. The old floor scaled it by the entry price instead, shaving
    another 15% off the fave-BUY error bar."""

    won_pnl, won_lost = trade_pnl(zone, bid, ask, 1)
    lost_pnl, lost_lost = trade_pnl(zone, bid, ask, 0)

    assert abs(abs(won_pnl - lost_pnl) - 1.0) < 1e-12
    assert won_lost != lost_lost


# --------------------------------------------------------------------------
# clustered_pooled: an error bar for the estimator actually reported
# --------------------------------------------------------------------------

def test_pooled_matches_clustered_when_every_cluster_holds_one_observation():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    _, _, _, plain = clustered(values, [f"M{i}" for i in range(len(values))])
    _, _, _, pooled = clustered_pooled(values, [1] * len(values))

    assert pooled == pytest.approx(plain)


def test_pooled_weights_big_clusters_and_cluster_means_do_not():
    """The defect: callers aggregated to cluster means and passed those to
    `clustered`, which put every value in its own group and returned the SE of
    the EQUAL-weighted mean of cluster means - while reporting the SIZE-weighted
    pooled mean as the point estimate. Two estimators, one t-statistic."""

    sums = [100.0, 2.0, 0.0, 4.0]
    counts = [100, 1, 1, 1]
    _, _, pooled_mean, pooled_se = clustered_pooled(sums, counts)
    means = [s / c for s, c in zip(sums, counts)]
    _, _, cluster_mean, cluster_se = clustered(means, ["a", "b", "c", "d"])

    # One 100-contract cluster and three singletons. The pooled mean is pinned
    # near the big cluster; the mean of cluster means is not.
    assert pooled_mean == pytest.approx(106.0 / 103.0)
    assert cluster_mean == pytest.approx(1.75)
    assert pooled_se < cluster_se


# --------------------------------------------------------------------------
# loss_count_floor: the guard that could not fire on the case it was written for
# --------------------------------------------------------------------------

def test_zero_loss_floor_is_set_by_clusters_not_contracts():
    """THE defect. The rule-of-three floor divided the loss-count uncertainty by
    the CONTRACT count, committing the same independence error the whole repo
    exists to avoid. On the shipped zero-loss tennis cell (865 contracts, 278
    clusters) that was the difference between a published t=11.0 and t=3.5."""

    n, groups = 865, 278
    floor = loss_count_floor([0] * groups, n)
    contract_denominated = 1.53 / n

    assert floor == pytest.approx(1.53 / groups, rel=1e-9)
    assert floor / contract_denominated == pytest.approx(n / groups, rel=1e-9)
    assert floor > 3 * contract_denominated


def test_clumped_losses_earn_a_wider_floor_than_scattered_ones():
    """Same number of losing contracts. Twelve losses inside two clusters is two
    events; twelve losses in twelve clusters is twelve. The first is far less
    informative about the rate and must price wider."""

    scattered = loss_count_floor([1] * 12 + [0] * 100, 500)
    clumped = loss_count_floor([6, 6] + [0] * 110, 500)

    assert clumped > scattered


def test_a_single_observed_loss_is_not_treated_as_precise():
    """sqrt(k) says sd=1.00 at one observed event. The exact Poisson bound says
    1.91. At the counts these trades produce, the asymptotic form is the
    difference between t=17 and t=11."""

    assert poisson_count_sd(0) == pytest.approx(1.53, rel=1e-3)
    assert poisson_count_sd(1) > 1.5

    for k in range(0, 40):
        assert poisson_count_sd(k) >= math.sqrt(k) - 1e-9


def test_the_floor_never_shrinks_the_sandwich():
    """It is a floor. A cell with plenty of losses keeps its empirical SE."""

    losses = [3, 2, 4, 1, 5, 2, 3, 4]
    floor = loss_count_floor(losses, 400)

    assert floor > 0


# --------------------------------------------------------------------------
# small-G inference
# --------------------------------------------------------------------------

def test_eighteen_clusters_do_not_get_the_normal_critical_value():
    assert cluster_t_critical(18) > 1.96
    assert cluster_t_critical(400) == pytest.approx(1.96)


# --------------------------------------------------------------------------
# clustering unit
# --------------------------------------------------------------------------

def test_one_underlying_is_one_cluster_across_its_series():
    """BTC and ETH ladders settling on the same day are not two independent
    draws, and neither are the Dow and the S&P."""

    assert cluster_key("crypto-hourly", "KXBTCD", "2026-08-20") == \
        cluster_key("crypto-15M", "KXETHD", "2026-08-20")
    assert cluster_key("indices", "KXDJI", "2026-08-20") == \
        cluster_key("indices", "KXSPX", "2026-08-20")


def test_separate_matches_stay_separate_clusters():
    assert cluster_key("tennis", "KXATPMATCH", "2026-08-20") != \
        cluster_key("tennis", "KXITFMATCH", "2026-08-20")


# --------------------------------------------------------------------------
# load_records: silent skips and silent duplicates
# --------------------------------------------------------------------------

def test_duplicate_tickers_across_files_are_dropped(tmp_path):
    """The four candle files in the study share ~1,240 tickers because targeted
    crawls re-drew markets an earlier pass already had. Pooling them counted
    those markets twice."""

    row = {"ticker": "T-1", "series": "KXA", "result": "yes",
           "close_ts": 1, "candles": []}
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text(json.dumps(row) + "\n")
    second.write_text(json.dumps(row) + "\n")

    records, stats = load_records([first, second])

    assert len(records) == 1
    assert stats["duplicates"] == 1
    assert stats["rows"] == 2


def test_a_missing_candle_file_is_an_error_not_a_shrug(tmp_path):
    """The defect: the builder skipped a non-existent path silently and still
    printed the file count it was asked for, so a typo produced a smaller table
    and no warning."""

    with pytest.raises(FileNotFoundError):
        load_records([tmp_path / "nope.jsonl"])


# --------------------------------------------------------------------------
# decay_panel: the horizons do not see the same markets
# --------------------------------------------------------------------------

def test_a_family_that_cannot_be_tested_at_a_horizon_says_so():
    """The defect: crypto-15M has no book an hour before close, so it silently
    vanished from the 30m and 60m rows while the page concluded "every tail
    trade in this study fails" the decay test. Untested is not failed."""

    close = 1_000_000
    records = [{"ticker": f"T-{i}", "series": "KXBTCD15M", "result": "no",
                "close_ts": close,
                "candles": [candle(close - 120, "0.02", "0.04")]}
               for i in range(80)]
    panel = decay_panel(records, "crypto-15M", "tail SELL")

    assert panel["missing_lookbacks"] == [5, 10, 30, 60]
    assert [r["lookback_min"] for r in panel["rows"]] == [2]


def test_the_overlap_between_horizons_is_reported():
    """`share_of_longest` is what tells a reader whether a decay curve is one
    population observed over time or five different casts."""

    close = 1_000_000
    records = []

    for i in range(40):
        candles = [candle(close - 130, "0.02", "0.04")]

        # only half the markets have a book an hour out
        if i % 2 == 0:
            candles.append(candle(close - 3610, "0.02", "0.04"))

        records.append({"ticker": f"T-{i}", "series": f"KXS{i}",
                        "result": "no", "close_ts": close, "candles": candles})

    panel = decay_panel(records, "other", "tail SELL")
    short = next(r for r in panel["rows"] if r["lookback_min"] == 2)

    assert short["markets"] == 40
    # 20 markets shared, 40 in the union: half the same markets, not "100%
    # of the long sample is in here" which is the reading that hides it.
    assert short["overlap_with_longest"] == pytest.approx(0.5)
    assert panel["balanced_markets"] == 20
