"""Suitability scoring, especially the queue-clearance term.

The case that motivated this module's rewrite: KXWNBATEAMTOTAL showed a 48c
spread on a book trading 56 contracts a day, and the old score ranked it first
on the exchange. The queue there needs weeks to clear. These tests pin down
that such a market scores zero however wide its spread.
"""

from kalshi_mm_bot.analytics.screening import MarketQuote
from kalshi_mm_bot.analytics.suitability import (
    MIN_QUEUE_CLEARANCE,
    assess,
    by_family,
)


def quote(
    *,
    ticker: str = "KXTEST-1",
    yes_bid: int = 400,
    yes_ask: int = 600,
    volume_24h: float = 100_000,
    seconds_to_close: float | None = 900.0,
    series: str = "KXTEST",
) -> MarketQuote:
    return MarketQuote(
        ticker=ticker,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        volume_24h=int(volume_24h),
        open_interest=0,
        seconds_to_close=seconds_to_close,
        series=series,
        title="",
    )


def test_wide_spread_in_a_dead_market_scores_zero() -> None:
    """The KXWNBATEAMTOTAL case: enormous spread, queue that never clears."""

    dead = assess(
        quote(yes_bid=2000, yes_ask=6850, volume_24h=56, seconds_to_close=46 * 3600),
        depth_at_touch=500.0,
    )

    assert dead.fee_viable, "the fee arithmetic alone still passes - that is the trap"
    assert dead.queue_viable is False
    assert dead.score() == 0.0


def test_an_unprobed_queue_is_not_a_passing_queue() -> None:
    """Missing depth must not read as 'clear'."""

    unmeasured = assess(quote(), depth_at_touch=None)

    assert unmeasured.expected_wait_seconds is None
    assert unmeasured.queue_clearance is None
    assert unmeasured.queue_viable is None
    assert unmeasured.score() == 0.0


def test_fast_recycling_short_dated_book_scores_above_zero() -> None:
    """The 15-minute crypto profile: thin queue against very heavy flow."""

    live = assess(
        quote(volume_24h=1_200_000, seconds_to_close=900.0),
        depth_at_touch=200.0,
    )

    assert live.queue_viable is True
    assert live.score() > 0.0


def test_expected_wait_halves_the_flow_because_we_sit_on_one_side() -> None:
    """86,400 contracts a day is 1/sec total, so 0.5/sec can reach our side."""

    assessment = assess(
        quote(volume_24h=86_400, seconds_to_close=3600.0), depth_at_touch=100.0
    )

    assert assessment.expected_wait_seconds == 200.0


def test_clearance_is_measured_against_the_close_when_it_binds_first() -> None:
    """A market closing in 10 minutes cannot use a six-hour session horizon."""

    closing = assess(
        quote(volume_24h=86_400, seconds_to_close=600.0), depth_at_touch=100.0
    )
    # 600s of market left against a 200s queue.
    assert closing.queue_clearance == 3.0
    assert closing.queue_viable is True


def test_a_distant_close_is_capped_at_one_session() -> None:
    """Six months of runway is not six months of opportunity to sit in a queue."""

    distant = assess(
        quote(volume_24h=86_400, seconds_to_close=180 * 86_400),
        depth_at_touch=100.0,
    )

    assert distant.queue_clearance == (6 * 3600) / 200.0


def test_clearance_bar_is_several_turnovers_not_one() -> None:
    """Being reached exactly at the bell is not a business."""

    assert MIN_QUEUE_CLEARANCE > 1.0

    marginal = assess(
        quote(volume_24h=86_400, seconds_to_close=250.0), depth_at_touch=100.0
    )
    # The queue clears once (250s vs a 200s wait) and still fails.
    assert marginal.queue_clearance is not None
    assert 1.0 < marginal.queue_clearance < MIN_QUEUE_CLEARANCE
    assert marginal.queue_viable is False
    assert marginal.score() == 0.0


def test_family_rollup_does_not_credit_unscorable_markets() -> None:
    assessments = [
        assess(quote(ticker="KXA-1", series="KXA"), depth_at_touch=None),
        assess(
            quote(ticker="KXB-1", series="KXB", volume_24h=1_200_000),
            depth_at_touch=200.0,
        ),
    ]

    families = {f.family: f for f in by_family(assessments)}

    assert families["KXA"].total_score == 0.0
    assert families["KXB"].total_score > 0.0
    # Ranked ahead of the unmeasured family.
    assert by_family(assessments)[0].family == "KXB"


def test_flow_is_measured_over_the_market_s_own_life_not_a_day() -> None:
    """A 15-minute window has no 24-hour history; assuming one understates its
    flow by a factor of ninety-six and makes every short-dated market look
    untradeable."""

    fast = assess(
        quote(volume_24h=12_000, seconds_to_close=900.0),
        depth_at_touch=200.0,
        age_seconds=900.0,
    )
    assumed_day = assess(
        quote(volume_24h=12_000, seconds_to_close=900.0),
        depth_at_touch=200.0,
    )

    assert fast.flow_window_seconds == 900.0
    assert assumed_day.flow_window_seconds == 86_400.0
    assert fast.expected_wait_seconds < assumed_day.expected_wait_seconds
    assert fast.queue_viable is True
    assert assumed_day.queue_viable is False, "the old assumption rejected it"


def test_flow_window_never_exceeds_a_day() -> None:
    """A month-old market did not accumulate today's volume over a month."""

    old = assess(
        quote(volume_24h=86_400, seconds_to_close=3600.0),
        depth_at_touch=100.0,
        age_seconds=30 * 86_400.0,
    )

    assert old.flow_window_seconds == 86_400.0
