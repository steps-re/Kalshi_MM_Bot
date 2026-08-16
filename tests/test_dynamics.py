from math import isclose, sqrt

from kalshi_mm_bot.market.dynamics import MarketDynamicsTracker, _forced_hold_probability
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.types import PriceRange

PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


def make_book(bid: str, ask: str, *, bid_size: str = "10.00", ask_size: str = "10.00") -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=((bid, bid_size),),
        asks_raw=((ask, ask_size),),
        price_ranges=PRICE_RANGES,
    )


def test_snapshot_is_none_for_a_one_sided_book() -> None:
    tracker = MarketDynamicsTracker()
    book = Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=(("0.5000", "10.00"),),
        asks_raw=(),
        price_ranges=PRICE_RANGES,
    )

    assert tracker.observe("M1", book, offset_seconds=0.0) is None


def test_snapshot_is_none_for_a_crossed_book() -> None:
    tracker = MarketDynamicsTracker()

    assert tracker.observe("M1", make_book("0.6000", "0.5000"), offset_seconds=0.0) is None


def test_microprice_leans_toward_the_thin_side() -> None:
    tracker = MarketDynamicsTracker()
    snapshot = tracker.observe(
        "M1",
        make_book("0.4000", "0.6000", bid_size="90.00", ask_size="10.00"),
        offset_seconds=0.0,
    )

    assert snapshot is not None
    # Heavy bid, thin ask: the next trade lifts the ask, so fair value sits high.
    assert snapshot.microprice > snapshot.mid
    assert snapshot.imbalance_bps > 0


def test_imbalance_is_zero_on_a_balanced_book() -> None:
    tracker = MarketDynamicsTracker()
    snapshot = tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=0.0)

    assert snapshot is not None
    assert snapshot.imbalance_bps == 0
    assert snapshot.microprice == snapshot.mid


def test_sigma_is_zero_when_the_mid_never_moves() -> None:
    tracker = MarketDynamicsTracker()

    for step in range(10):
        snapshot = tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=step)

    assert snapshot is not None
    assert snapshot.sigma_ticks_per_sqrt_sec == 0.0
    assert snapshot.expected_move_ticks(4.0) == 0.0


def test_sigma_tracks_a_known_random_walk() -> None:
    tracker = MarketDynamicsTracker()
    # The book alternates between (0.40, 0.42) and (0.42, 0.44), so the mid
    # steps 200 ticks every second. Realized variance is 200^2 per second and
    # sigma should come out at 200 ticks per sqrt(second).
    prices = ["0.4000", "0.4200", "0.4000", "0.4200", "0.4000", "0.4200"]

    for step, bid in enumerate(prices):
        ask = f"{float(bid) + 0.02:.4f}"
        snapshot = tracker.observe("M1", make_book(bid, ask), offset_seconds=float(step))

    assert snapshot is not None
    assert isclose(snapshot.sigma_ticks_per_sqrt_sec, 200.0, rel_tol=0.01)
    assert isclose(snapshot.expected_move_ticks(4.0), 400.0, rel_tol=0.01)


def test_terminal_sigma_peaks_at_the_midpoint() -> None:
    tracker = MarketDynamicsTracker()

    middle = tracker.observe("M1", make_book("0.4900", "0.5100"), offset_seconds=0.0)
    tracker.reset()
    tail = tracker.observe("M1", make_book("0.0400", "0.0600"), offset_seconds=0.0)

    assert middle is not None and tail is not None
    assert isclose(middle.terminal_sigma_ticks(), 0.5 * 10000, rel_tol=0.01)
    assert tail.terminal_sigma_ticks() < middle.terminal_sigma_ticks() / 2


def test_inventory_risk_blends_toward_the_binary_payoff_near_close() -> None:
    tracker = MarketDynamicsTracker()

    for step in range(6):
        bid = "0.4900" if step % 2 else "0.5000"
        ask = f"{float(bid) + 0.02:.4f}"
        far = tracker.observe(
            "M1",
            make_book(bid, ask),
            offset_seconds=float(step),
            seconds_to_close=3600.0,
        )

    tracker.reset()

    for step in range(6):
        bid = "0.4900" if step % 2 else "0.5000"
        ask = f"{float(bid) + 0.02:.4f}"
        near = tracker.observe(
            "M1",
            make_book(bid, ask),
            offset_seconds=float(step),
            seconds_to_close=1.0,
        )

    assert far is not None and near is not None
    # Same volatility, same price. The only difference is that the near-close
    # position probably cannot be flattened, so its risk is the coin flip.
    assert near.inventory_sigma_ticks(flatten_seconds=30.0) > far.inventory_sigma_ticks(
        flatten_seconds=30.0
    )


def test_inventory_risk_ignores_expiry_when_close_time_is_unknown() -> None:
    tracker = MarketDynamicsTracker()

    for step in range(6):
        snapshot = tracker.observe("M1", make_book("0.4900", "0.5100"), offset_seconds=float(step))

    assert snapshot is not None
    assert snapshot.inventory_sigma_ticks(flatten_seconds=30.0) == snapshot.expected_move_ticks(
        30.0
    )


def test_forced_hold_probability_spans_zero_to_one() -> None:
    assert _forced_hold_probability(600.0, 30.0) == 0.0
    assert _forced_hold_probability(30.0, 30.0) == 0.0
    assert _forced_hold_probability(0.0, 30.0) == 1.0
    assert isclose(_forced_hold_probability(15.0, 30.0), 0.5)


def test_update_rate_reflects_event_density() -> None:
    tracker = MarketDynamicsTracker()

    for step in range(11):
        snapshot = tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=step * 0.5)

    assert snapshot is not None
    assert isclose(snapshot.update_rate_hz, 2.0, rel_tol=0.01)


def test_window_trims_stale_samples() -> None:
    tracker = MarketDynamicsTracker(vol_window_seconds=5.0, min_samples=2)

    for step in range(50):
        snapshot = tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=float(step))

    assert snapshot is not None
    assert snapshot.sample_count <= 8


def test_reset_clears_only_the_named_ticker() -> None:
    tracker = MarketDynamicsTracker()
    tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=0.0)
    tracker.observe("M2", make_book("0.4000", "0.6000"), offset_seconds=0.0)
    tracker.observe("M2", make_book("0.4000", "0.6000"), offset_seconds=1.0)

    tracker.reset("M1")
    m1 = tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=2.0)
    m2 = tracker.observe("M2", make_book("0.4000", "0.6000"), offset_seconds=2.0)

    assert m1 is not None and m2 is not None
    assert m1.sample_count == 1
    assert m2.sample_count == 3


def test_snapshot_carries_the_market_price_grid() -> None:
    tracker = MarketDynamicsTracker()
    snapshot = tracker.observe("M1", make_book("0.4000", "0.6000"), offset_seconds=0.0)

    assert snapshot is not None
    assert snapshot.price_levels[:3] == (0, 100, 200)


def test_expected_move_scales_with_square_root_of_time() -> None:
    tracker = MarketDynamicsTracker()

    for step in range(6):
        bid = "0.4900" if step % 2 else "0.5000"
        ask = f"{float(bid) + 0.02:.4f}"
        snapshot = tracker.observe("M1", make_book(bid, ask), offset_seconds=float(step))

    assert snapshot is not None
    one = snapshot.expected_move_ticks(1.0)
    four = snapshot.expected_move_ticks(4.0)

    assert isclose(four, one * sqrt(4.0), rel_tol=1e-9)
