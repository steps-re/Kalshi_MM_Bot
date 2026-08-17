"""The order journal, and the loop it closes.

The campaign monitor halts when it cannot measure adverse selection, and adverse
selection needs the mid at fill time. Nothing in the live path recorded it, so
the monitor halted every live run after its grace period - correctly, and
permanently. These tests are that gap being closed.
"""

import asyncio

from kalshi_mm_bot.live import LiveOrderManager
from kalshi_mm_bot.live.campaign import CampaignMonitor, CampaignSample, Verdict
from kalshi_mm_bot.live.journal import (
    OrderJournal,
    book_mid,
    depth_ahead,
    fills_for_monitor,
    quote_count,
    read_journal,
)
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp
from kalshi_mm_bot.market.types import PriceRange

ONE = COUNT_SCALE
PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


def book(bid="0.4900", ask="0.5000", bid_size="500.00", ask_size="400.00"):
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=((bid, bid_size),),
        asks_raw=((ask, ask_size),),
        price_ranges=PRICE_RANGES,
    )


def test_mid_needs_two_sides() -> None:
    """Marking against a one-sided book is marking against our own optimism."""

    assert book_mid(book()) == parse_price_fp("0.4950")
    assert book_mid(None) is None

    one_sided = Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=(("0.4900", "500.00"),),
        asks_raw=(),
        price_ranges=PRICE_RANGES,
    )
    assert book_mid(one_sided) is None


def test_depth_ahead_reads_our_own_price_level() -> None:
    """Queue position at placement, which cannot be reconstructed later."""

    # Fixed point: 500.00 contracts resting is 50_000, matching the book.
    assert depth_ahead(book(), yes_price=parse_price_fp("0.4900"), is_buy=True) == 500 * ONE
    assert depth_ahead(book(), yes_price=parse_price_fp("0.5000"), is_buy=False) == 400 * ONE
    # A price nobody is resting at is empty, not unknown.
    assert depth_ahead(book(), yes_price=parse_price_fp("0.4000"), is_buy=True) in (0.0, None)


def test_journal_records_the_three_moments(tmp_path) -> None:
    path = tmp_path / "journal.jsonl"
    journal = OrderJournal(path=path)

    journal.record_placed(
        order_id="o1", market_ticker="M1", action="buy",
        yes_price=parse_price_fp("0.4900"), count=ONE, book=book(),
    )
    journal.record_filled(
        order_id="o1", trade_id="t1", market_ticker="M1", action="buy",
        yes_price=parse_price_fp("0.4900"), count=ONE, is_taker=False,
        fee_micros=0, book=book(),
    )
    journal.record_cancelled(order_id="o2", market_ticker="M1", reason="replaced")
    journal.close()

    events = read_journal(path)

    assert [e["event"] for e in events] == ["placed", "filled", "cancelled"]
    assert events[0]["depth_ahead"] == 500 * ONE
    assert events[1]["mid_at_fill"] == parse_price_fp("0.4950")
    assert all("at" in e for e in events)


def test_an_unreadable_fee_survives_as_none_all_the_way_to_the_monitor() -> None:
    """None is not zero. A fee we could not read once made 48 taker fills that
    really cost $0.5879 look free."""

    journal = OrderJournal()
    journal.record_filled(
        order_id="o1", trade_id="t1", market_ticker="M1", action="buy",
        yes_price=parse_price_fp("0.5000"), count=ONE, is_taker=True,
        fee_micros=None, book=book(),
    )

    fills = fills_for_monitor(journal.events)

    assert fills[0].fee_micros is None


def test_a_partial_last_line_does_not_break_reading(tmp_path) -> None:
    """A live writer's file always ends mid-line at some point."""

    path = tmp_path / "journal.jsonl"
    journal = OrderJournal(path=path)
    journal.record_cancelled(order_id="o1", market_ticker="M1")
    journal.close()

    with path.open("a") as handle:
        handle.write('{"event": "filled", "order_id": "o2"')

    assert len(read_journal(path)) == 1


def test_the_journal_unblocks_the_markout_tripwire() -> None:
    """The whole point: the monitor can finally evaluate adverse selection.

    Without a mid at fill time this tripwire reports UNKNOWN and halts the
    campaign after its grace period, whatever the strategy does.
    """

    journal = OrderJournal()

    for index in range(25):
        journal.record_placed(
            order_id=f"o{index}", market_ticker="M1", action="buy",
            yes_price=parse_price_fp("0.4900"), count=ONE, book=book(),
        )
        journal.record_filled(
            order_id=f"o{index}", trade_id=f"t{index}", market_ticker="M1",
            action="buy", yes_price=parse_price_fp("0.4900"), count=ONE,
            is_taker=False, fee_micros=0, book=book(),
        )

    journal.record_filled(
        order_id="x", trade_id="tx", market_ticker="M1", action="buy",
        yes_price=parse_price_fp("0.5000"), count=ONE, is_taker=True,
        fee_micros=17_500, book=book(),
    )

    sample = CampaignSample(
        fills=fills_for_monitor(journal.events),
        balance_micros=50_000_000,
        realized_pnl_micros=0,
        elapsed_seconds=7200.0,
        quotes_placed=quote_count(journal.events),
    )
    verdict = CampaignMonitor().assess(sample)
    markout = next(r for r in verdict.readings if r.key == "markout")

    assert markout.verdict is not Verdict.UNKNOWN
    assert not verdict.should_halt


def test_the_manager_journals_a_fill_against_the_book_it_happened_in() -> None:
    """The manager is handed books rather than holding them, so it caches the
    last one - otherwise the book is gone by the time the fill arrives."""

    from tests.test_live_trader import FakeRest, buy_intent

    async def run() -> None:
        journal = OrderJournal()
        manager = LiveOrderManager(FakeRest(), dry_run=True, journal=journal)

        manager.observe_orderbook(book())
        await manager.sync_quotes("M1", [buy_intent("0.4900")], now=1)

        placed = [e for e in journal.events if e["event"] == "placed"]
        assert placed and placed[0]["depth_ahead"] == 500 * ONE
        assert placed[0]["mid"] == parse_price_fp("0.4950")

    asyncio.run(run())


def test_a_fill_is_journalled_even_when_its_order_is_gone() -> None:
    """Measured on the first live run: 116 fills, 3 recorded.

    With 1,280 replacements, almost every fill arrived for an order that had
    already been cancelled or popped, and journalling inside the order lookup
    dropped all of them. The exchange's fill message carries everything needed.
    """

    from kalshi_mm_bot.market.types import OrderFill
    from tests.test_live_trader import FakeRest

    journal = OrderJournal()
    manager = LiveOrderManager(FakeRest(), dry_run=True, journal=journal)
    manager.observe_orderbook(book())

    assert not manager.orders, "no order is tracked - that is the point"

    manager.handle_fill(
        OrderFill(
            trade_id="t1",
            order_id="an-order-we-no-longer-hold",
            market_ticker="M1",
            is_taker=False,
            side="yes",
            action="buy",
            yes_price=parse_price_fp("0.4900"),
            count=ONE,
            post_position=ONE,
        )
    )

    filled = [e for e in journal.events if e["event"] == "filled"]
    assert len(filled) == 1
    assert filled[0]["mid_at_fill"] == parse_price_fp("0.4950")
