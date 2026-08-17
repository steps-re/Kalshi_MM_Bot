"""The sweep's reporting, which decides which strategy looks best.

This tool has produced three wrong numbers already - capture labelled in cents
while holding dollars, residual inventory summed across tickers so ten small
positions read as one huge one, and a ranking on capture-per-fill that promoted
a configuration earning a third as much as its neighbour. None were caught by a
test, because there were none. These are that test.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from sweep_backtests import MIN_FILLS_FOR_CONFIDENCE, Run, report  # noqa: E402

MICROS = 1_000_000


def run(
    *,
    strategy="s",
    fills=1000,
    capture_dollars=10.0,
    inventory_dollars=0.0,
    residual=0.0,
    markets=1,
    recording="rec",
):
    return Run(
        recording=recording,
        strategy=strategy,
        fills=fills,
        spread_capture=int(capture_dollars * MICROS),
        inventory=int(inventory_dollars * MICROS),
        net=int((capture_dollars + inventory_dollars) * MICROS),
        residual_contracts=residual,
        markets_held=markets,
    )


def test_ranks_on_money_earned_not_on_the_per_fill_rate() -> None:
    """The horizon tuning case: per-fill promoted a config earning a third as much.

    edge=100 earned $6.15 over 540 fills; edge=150 earned $2.10 over 165. The
    second has the better rate and the first has the money.
    """

    rows = [
        run(strategy="earns_more", fills=540, capture_dollars=6.15),
        run(strategy="better_rate", fills=165, capture_dollars=2.10),
    ]

    lines = report(rows).splitlines()
    order = [line.split()[0] for line in lines[1:3]]

    assert order[0] == "earns_more"


def test_a_thin_sample_is_flagged_rather_than_promoted() -> None:
    thin = [run(strategy="rare", fills=MIN_FILLS_FOR_CONFIDENCE - 1, capture_dollars=50.0)]

    text = report(thin)

    assert "!!" in text
    assert "rare" in text
    assert "rarer one" in text


def test_a_healthy_sample_is_not_flagged() -> None:
    ample = [run(strategy="busy", fills=MIN_FILLS_FOR_CONFIDENCE + 1)]

    assert "!!" not in report(ample)


def test_residual_is_per_market_so_spread_inventory_is_not_read_as_a_limit() -> None:
    """Eight contracts in each of ten markets is not eighty in one."""

    spread_out = run(residual=80.0, markets=10)
    concentrated = run(residual=80.0, markets=1)

    assert spread_out.residual_per_market == 8.0
    assert concentrated.residual_per_market == 80.0


def test_a_run_holding_nothing_counts_as_flat() -> None:
    flat = [run(strategy="flat", residual=0.0, markets=0)]

    assert "1/1" in report(flat)


def test_a_run_at_its_position_limit_is_not_counted_as_flat() -> None:
    loaded = [run(strategy="loaded", residual=50.0, markets=1)]

    assert "0/1" in report(loaded)


def test_capture_is_reported_in_dollars() -> None:
    """It was labelled in cents while holding dollars - a 100x display error."""

    text = report([run(capture_dollars=61.22, fills=1690)])

    assert "61.22$" in text


def test_per_fill_is_reported_in_cents() -> None:
    # $10 over 1000 fills is one cent each.
    text = report([run(capture_dollars=10.0, fills=1000)])

    assert "1.00c" in text


def test_zero_fill_strategies_do_not_crash_the_report() -> None:
    """A strategy can legitimately place nothing - horizon did for a whole run."""

    text = report([run(strategy="silent", fills=0, capture_dollars=0.0)])

    assert "silent" in text


def _resolution(deltas=100_000, seconds=900.0, tickers=1, rates=None):
    from sweep_backtests import Resolution

    # Per-ticker rates are what the verdict keys off. Default to spreading the
    # deltas evenly, which is what a single-market recording really looks like.
    if rates is None:
        rates = tuple([deltas / max(1.0, seconds) / max(1, tickers)] * max(1, tickers))

    return Resolution(
        recording="rec",
        deltas=deltas,
        seconds=seconds,
        tickers=tickers,
        ticker_rates=tuple(sorted(rates, reverse=True)),
    )


def test_a_mixed_recording_is_judged_on_its_best_market_not_its_average() -> None:
    """Two busy 15-minute windows beside a quiet strike ladder.

    Judging that on the average discards exactly the data it was collected for:
    measured, the windows recorded at 250-360 deltas/sec while the ladder sat
    near 5, and the aggregate read as thin.
    """

    mixed = _resolution(rates=(336.5, 124.3, 5.5), tickers=3)

    assert not mixed.is_thin
    assert mixed.best_ticker_rate == 336.5
    # The median still describes the recording as a whole.
    assert mixed.deltas_per_second_per_ticker == 124.3


def test_a_polled_book_is_flagged_as_too_thin_to_prove_fill_rates() -> None:
    """Measured: polling carried 11.8% of the real shrinkage, and the same
    strategy filled 13 times instead of 942."""

    from sweep_backtests import resolution_report

    polled = _resolution(deltas=31_013, seconds=898.0, tickers=10)

    assert polled.deltas_per_second_per_ticker < 5
    assert polled.is_thin

    text = resolution_report([polled])
    assert "!!" in text
    assert "floors, not estimates" in text


def test_a_websocket_book_is_not_flagged() -> None:
    from sweep_backtests import resolution_report

    feed = _resolution(deltas=300_562, seconds=854.0, tickers=1)

    assert feed.deltas_per_second_per_ticker > 300
    assert not feed.is_thin
    assert resolution_report([feed]) == ""


def test_resolution_is_per_ticker_so_breadth_does_not_pass_as_depth() -> None:
    """The same delta count spread over ten markets is a tenth the resolution."""

    one = _resolution(deltas=100_000, tickers=1)
    ten = _resolution(deltas=100_000, tickers=10)

    assert ten.deltas_per_second_per_ticker == one.deltas_per_second_per_ticker / 10


def test_short_window_series_are_pinned_for_recording() -> None:
    """Churn ranking fills every slot with quiet daily strike ladders.

    Measured on one cycle: the churn-picked markets carried 4-5 book
    deltas/sec/ticker while the 15-minute windows carried 106 and 352. Recording
    the quiet markets in high fidelity is not an improvement over recording them
    badly, and the short windows are the subject of the research besides.
    """

    import collect_loop

    assert collect_loop._is_short_window("KXBTC15M-26AUG170700-00")
    assert collect_loop._is_short_window("KXETH15M-26AUG170700-00")
    assert not collect_loop._is_short_window("KXBTCD-26AUG1717-T62999.99")
    assert not collect_loop._is_short_window("KXNFLGAME-26AUG22DALARI-ARI")


def _candidate(depth=1819.0, volume=5468.0, age=522.0, life=378.0, mid=2300):
    from capacity_scan import Candidate

    return Candidate(
        ticker="KXBTC15M-TEST",
        series="KXBTC15M",
        mid=mid,
        spread_ticks=100,
        depth=depth,
        volume_contracts=volume,
        age_seconds=age,
        seconds_to_close=life,
    )


def test_queue_wait_credits_cancellations_not_only_trades() -> None:
    """The correction that made this screen agree with reality.

    Measured off the websocket feed, 84% of a level's shrinkage is people
    cancelling, and an order ahead of us that cancels advances us exactly as
    much as one that trades. Dividing depth by traded flow alone overstates the
    wait about six times - it rated KXBTC15M unreachable at a 347s wait against
    a 378s life, in a market we had traded that afternoon for 116 fills.
    """

    from capacity_scan import TRADE_FRACTION

    btc = _candidate()

    trades_only = btc.depth / btc.flow_per_second
    assert trades_only > 300, "the naive model says unreachable"

    assert btc.wait_seconds < 60, "crediting cancellations says otherwise"
    assert btc.reachable
    # The whole difference is the measured trade fraction.
    assert abs(btc.wait_seconds - trades_only * TRADE_FRACTION) < 1e-6


def test_a_long_lived_dead_market_is_not_reachable() -> None:
    """A 45-hour life must not excuse a hopeless queue.

    With only a fractional test, four dead esports books qualified on waits of
    9,000-38,000 seconds purely because they lived long enough for that to look
    proportionate.
    """

    dead = _candidate(depth=511.0, volume=200.0, age=86_400.0, life=45 * 3600.0)

    assert dead.wait_seconds > 300
    assert not dead.reachable


def test_a_queue_clearing_only_at_the_bell_is_not_reachable() -> None:
    """Reaching the front exactly as the market closes is not a business."""

    late = _candidate(depth=1819.0, volume=5468.0, age=522.0, life=60.0)

    assert late.wait_seconds < 300
    assert not late.reachable


def test_window_alignment_targets_the_quarter_hour() -> None:
    """Cycles must start where a 15-minute window opens.

    Recording on a fixed clock produced a book with 95% of its fills inside six
    minutes of close and nothing at all in the 6-12 minute band, which is where
    live markout is +0.41c. The backtests built on it were measuring the regime
    the strategy should avoid.
    """

    import collect_loop

    assert collect_loop.seconds_to_next_window(0) == 900
    assert collect_loop.seconds_to_next_window(1) == 899
    assert collect_loop.seconds_to_next_window(899) == 1
    # Exactly on a boundary means a full window is available, not zero.
    assert collect_loop.seconds_to_next_window(900) == 900


def test_a_window_expiring_right_now_is_not_pinned() -> None:
    """The bug the alignment fix created.

    Waiting for the quarter hour and then selecting grabs the window that just
    expired at that boundary - Kalshi lists it for a few seconds more. One
    aligned cycle recorded two dead markets for fourteen minutes: 2,389 events
    where a live window produces ~100,000.
    """

    from datetime import UTC, datetime, timedelta

    import collect_loop

    now = datetime(2026, 8, 17, 16, 45, 24, tzinfo=UTC)
    just_closed = {
        "ticker": "KXBTC15M-26AUG171245-45",
        "close_time": "2026-08-17T16:45:00Z",
    }
    fresh = {
        "ticker": "KXBTC15M-26AUG171300-00",
        "close_time": (now + timedelta(minutes=14)).isoformat().replace("+00:00", "Z"),
    }

    assert not collect_loop._pinnable(just_closed, now)
    assert collect_loop._pinnable(fresh, now)
    # Still a short window by name - the name was never the problem.
    assert collect_loop._is_short_window(just_closed["ticker"])


def test_a_freshly_opened_window_survives_the_volume_floor() -> None:
    """Aligning to window open means looking at a market seconds after it opens,
    when it has traded almost nothing. A volume filter then rejects exactly the
    market the alignment exists to capture."""

    import collect_loop

    assert collect_loop._is_short_window("KXBTC15M-26AUG171300-00")
    # Not a pinned series, so the floor still applies to it.
    assert not collect_loop._is_short_window("KXBTCD-26AUG1717-T63499.99")
