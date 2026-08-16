from kalshi_mm_bot.live.risk import BreachKind, RiskLimits, RiskMonitor
from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE

CLEAN = {"position": 0, "equity_micros": 0}


def test_no_limits_means_no_breach() -> None:
    monitor = RiskMonitor(limits=RiskLimits(max_feed_silence_seconds=None))

    assert monitor.check(**CLEAN, now=1000.0) is None
    assert not monitor.halted


def test_position_limit_trips() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_abs_position=5 * COUNT_SCALE, max_feed_silence_seconds=None)
    )

    breach = monitor.check(position=6 * COUNT_SCALE, equity_micros=0, now=1.0)

    assert breach is not None
    assert breach.kind is BreachKind.POSITION
    assert monitor.halted


def test_position_limit_is_symmetric() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_abs_position=5 * COUNT_SCALE, max_feed_silence_seconds=None)
    )

    assert monitor.check(position=-6 * COUNT_SCALE, equity_micros=0, now=1.0) is not None


def test_session_loss_limit_trips() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(
            max_session_loss_micros=5 * MONEY_SCALE,
            max_feed_silence_seconds=None,
        )
    )

    assert monitor.check(position=0, equity_micros=-4 * MONEY_SCALE, now=1.0) is None
    breach = monitor.check(position=0, equity_micros=-6 * MONEY_SCALE, now=2.0)

    assert breach is not None
    assert breach.kind is BreachKind.SESSION_LOSS


def test_drawdown_is_measured_from_the_peak_not_from_zero() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_drawdown_micros=2 * MONEY_SCALE, max_feed_silence_seconds=None)
    )

    # Up ten dollars, then back to eight: profitable overall, but a two dollar
    # drawdown. A limit measured from zero would never notice.
    assert monitor.check(position=0, equity_micros=10 * MONEY_SCALE, now=1.0) is None
    breach = monitor.check(position=0, equity_micros=7 * MONEY_SCALE, now=2.0)

    assert breach is not None
    assert breach.kind is BreachKind.DRAWDOWN


def test_order_rate_limit_trips_and_window_rolls() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_orders_per_minute=3, max_feed_silence_seconds=None)
    )

    for index in range(4):
        monitor.record_order(now=100.0 + index)

    assert monitor.check(**CLEAN, now=104.0) is not None


def test_old_orders_leave_the_rate_window() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_orders_per_minute=3, max_feed_silence_seconds=None)
    )

    for index in range(4):
        monitor.record_order(now=100.0 + index)

    # Two minutes later those orders no longer count.
    assert monitor.check(**CLEAN, now=230.0) is None


def test_consecutive_rejections_trip_and_reset_on_success() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_consecutive_rejections=2, max_feed_silence_seconds=None)
    )

    monitor.record_rejection()
    monitor.record_rejection()
    assert monitor.check(**CLEAN, now=1.0) is None

    monitor.record_acceptance()
    monitor.record_rejection()
    assert monitor.check(**CLEAN, now=2.0) is None

    monitor.record_rejection()
    monitor.record_rejection()
    breach = monitor.check(**CLEAN, now=3.0)

    assert breach is not None
    assert breach.kind is BreachKind.REJECTIONS


def test_feed_silence_trips_when_no_data_arrives() -> None:
    monitor = RiskMonitor(limits=RiskLimits(max_feed_silence_seconds=10.0))
    monitor.record_event(now=100.0)

    assert monitor.check(**CLEAN, now=105.0) is None
    breach = monitor.check(**CLEAN, now=120.0)

    assert breach is not None
    assert breach.kind is BreachKind.FEED_SILENCE
    assert "stale" in breach.detail


def test_feed_silence_is_measured_from_session_start_before_any_event() -> None:
    # A feed that never delivers anything must still trip, not sit forever
    # waiting for a first event that will not come.
    monitor = RiskMonitor(limits=RiskLimits(max_feed_silence_seconds=10.0))
    start = monitor._started_monotonic

    assert monitor.check(**CLEAN, now=start + 30.0) is not None


def test_halt_is_permanent() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_abs_position=1 * COUNT_SCALE, max_feed_silence_seconds=None)
    )
    monitor.check(position=5 * COUNT_SCALE, equity_micros=0, now=1.0)

    # Even with everything clean, a halted monitor stays halted.
    breach = monitor.check(**CLEAN, now=2.0)

    assert breach is not None
    assert breach.kind is BreachKind.POSITION
    assert monitor.halt_reason is not None


def test_conservative_preset_sets_every_limit() -> None:
    limits = RiskLimits.conservative(contracts=10, loss_dollars=5.0)

    assert limits.max_abs_position == 10 * COUNT_SCALE
    assert limits.max_session_loss_micros == 5 * MONEY_SCALE
    assert limits.max_feed_silence_seconds == 15.0


def test_breach_describes_itself_as_a_halt() -> None:
    monitor = RiskMonitor(
        limits=RiskLimits(max_abs_position=0, max_feed_silence_seconds=None)
    )
    breach = monitor.check(position=1 * COUNT_SCALE, equity_micros=0, now=1.0)

    assert breach is not None
    assert breach.describe().startswith("[HALT] position:")
