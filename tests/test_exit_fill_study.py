"""The fill detector has to bracket the truth, and the old one did not.

Book snapshots cannot see trades, so a resting-fill criterion built from them is
a proxy. The previous proxy - "the best bid rose to our ask" - misses the most
ordinary fill there is, and misses it in the direction that made the candidate
look dead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from exit_fill_study import book_at, simulate  # noqa: E402

# A `walk` row is (offset, mid, obi, bid, ask, bid_sz, ask_sz).
HOLD = 1.0
REST = 10.0


def row(offset, bid, ask, bid_sz=5000, ask_sz=5000):
    return (offset, (bid + ask) / 2, 0.0, bid, ask, bid_sz, ask_sz)


def run(rows, *, buying=True, entry=300):
    offsets = [r[0] for r in rows]
    return simulate(rows, offsets, 0, HOLD, REST, buying, entry)


def test_a_swept_level_is_a_fill_even_when_the_bid_never_crosses():
    """The regression. A marketable buy consumes the whole ask at our price and
    the book re-quotes one tick higher with the bid still below us. That IS a
    fill of a resting sell. The old detector required best_bid >= our ask, so it
    scored this as a forced cross and charged the trade an exit fee it would
    never have paid.
    """

    rows = [
        row(0.0, 295, 300),
        row(1.0, 295, 300),      # exit posted here, resting a sell at 300
        row(2.0, 296, 400),      # our level is gone, bid still far below 300
        row(3.0, 296, 400),
    ]
    optimistic, conservative, _when, filled, crossed = run(rows)

    assert optimistic is True
    # Nothing traded up THROUGH us, so the conservative bound stays false. The
    # truth is between the two, which is the whole point of reporting both.
    assert conservative is False
    assert filled != crossed


def test_trading_up_through_the_level_satisfies_both_bounds():
    rows = [
        row(0.0, 295, 300),
        row(1.0, 295, 300),
        row(2.0, 305, 310),      # market traded up through 300
        row(3.0, 305, 310),
    ]
    optimistic, conservative, when, _filled, _crossed = run(rows)

    assert (optimistic, conservative) == (True, True)
    assert when == 1.0


def test_a_quiet_book_fills_neither_way():
    rows = [row(offset, 295, 300) for offset in (0.0, 1.0, 2.0, 3.0)]
    optimistic, conservative, when, _filled, _crossed = run(rows)

    assert (optimistic, conservative, when) == (False, False, None)


def test_partial_depletion_counts_only_toward_the_upper_bound():
    """Someone traded at our price but the level did not empty. A back-of-queue
    order may not have been reached, so this belongs in the optimistic bound and
    nowhere else."""

    rows = [
        row(0.0, 295, 300),
        row(1.0, 295, 300, ask_sz=5000),
        row(2.0, 295, 300, ask_sz=1000),
        row(3.0, 295, 300, ask_sz=1000),
    ]
    optimistic, conservative, _when, _filled, _crossed = run(rows)

    assert (optimistic, conservative) == (True, False)


def test_the_exit_book_is_the_last_sample_before_the_hold_not_the_first_after():
    """Lookahead defect #1, which set both the price we rest at and the P&L.

    The book at t+hold is the last update at or before it. Reading the next
    update instead peeks at the move the entry signal is trying to predict.
    """

    rows = [
        row(0.0, 295, 300),
        row(0.9, 295, 300),      # the honest exit book
        row(4.0, 380, 390),      # a big move AFTER the hold expired
        row(9.0, 380, 390),
    ]
    _optimistic, _conservative, _when, filled, _crossed = run(rows)

    # Resting at 300 against a 300-tick entry is a flat trade minus the entry
    # fee. Had the exit priced off the 390 ask it would have booked a windfall.
    assert filled < 0.1


def test_book_at_never_reads_forward():
    rows = [row(0.0, 100, 110), row(5.0, 200, 210), row(9.0, 300, 310)]
    offsets = [r[0] for r in rows]

    assert book_at(offsets, rows, 4.9)[3] == 100
    assert book_at(offsets, rows, 5.0)[3] == 200
    assert book_at(offsets, rows, 100.0)[3] == 300
    assert book_at(offsets, rows, -1.0) is None


def test_a_resting_buy_mirrors_the_sell_logic():
    """Short exiting by buying at the bid: the level clearing means the best bid
    dropped away from our price."""

    rows = [
        row(0.0, 9700, 9705),
        row(1.0, 9700, 9705),    # resting a buy at 9700
        row(2.0, 9695, 9705),    # the 9700 bid level is gone
        row(3.0, 9695, 9705),    # but no seller came down TO 9700
    ]
    offsets = [r[0] for r in rows]
    optimistic, conservative, _when, _filled, _crossed = simulate(
        rows, offsets, 0, HOLD, REST, False, 9700)

    assert (optimistic, conservative) == (True, False)


def test_a_seller_coming_down_through_a_resting_buy_is_traded_through():
    """The mirror of trading up through a resting sell: the ask drops to or
    below our bid, so anything resting there is gone."""

    rows = [
        row(0.0, 9700, 9705),
        row(1.0, 9700, 9705),
        row(2.0, 9600, 9605),    # ask 9605 is below our 9700 bid
        row(3.0, 9600, 9605),
    ]
    offsets = [r[0] for r in rows]
    optimistic, conservative, _when, _filled, _crossed = simulate(
        rows, offsets, 0, HOLD, REST, False, 9700)

    assert (optimistic, conservative) == (True, True)
