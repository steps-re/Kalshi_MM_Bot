import asyncio

import pytest

from kalshi_mm_bot.live import LiveOrderManager, LivePortfolio
from kalshi_mm_bot.live.trader import _cancel_on_shutdown
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp
from kalshi_mm_bot.market.types import MarketPosition, OrderFill, PriceRange
from kalshi_mm_bot.strategy import DumbMarketMakerStrategy
from kalshi_mm_bot.strategy.requote import RequotePolicy
from kalshi_mm_bot.strategy.types import QuoteIntent, StrategyContext


PRICE_RANGES = (PriceRange(start=0, end=10000, step=100),)


class FakeRest:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.canceled: list[dict] = []
        self.create_response: dict | None = None
        self.cancel_response: dict | None = None
        self.reverse_create_response = False
        self.cancel_failures: set[str] = set()
        self.resting_orders_by_ticker: dict[str, list[dict]] = {}
        self.available_balance_cents = 1_000_000

    async def batch_create_orders(self, orders) -> dict:
        payloads = [order.to_json() for order in orders]
        self.created.extend(payloads)

        if self.create_response is not None:
            return self.create_response

        response_orders = [
            {
                "order_id": f"live-{index}",
                "client_order_id": payload["client_order_id"],
            }
            for index, payload in enumerate(payloads, start=1)
        ]

        if self.reverse_create_response:
            response_orders.reverse()

        return {"orders": response_orders}

    async def batch_cancel_orders(self, orders) -> dict:
        payloads = [order.to_json() for order in orders]
        self.canceled.extend(payloads)

        for payload in payloads:
            if payload["order_id"] in self.cancel_failures:
                raise RuntimeError(f"cancel failed for {payload['order_id']}")

        if self.cancel_response is not None:
            return self.cancel_response

        return {
            "orders": [
                {
                    "order_id": payload["order_id"],
                    "client_order_id": "",
                    "reduced_by": "1.00",
                }
                for payload in payloads
            ]
        }

    async def get_available_balance_cents(self) -> int:
        return self.available_balance_cents

    async def get_orders(self, **kwargs) -> list[dict]:
        return list(self.resting_orders_by_ticker.get(kwargs.get("ticker"), ()))


def buy_intent(
    price: str = "0.5000",
    count: int = COUNT_SCALE,
    quote_id: str = "M1:adaptive:yes:buy",
) -> QuoteIntent:
    return QuoteIntent(
        quote_id=quote_id,
        market_ticker="M1",
        action="buy",
        side="yes",
        yes_price=parse_price_fp(price),
        count=count,
    )


def sell_intent(
    price: str = "0.5100",
    count: int = COUNT_SCALE,
    quote_id: str = "M1:adaptive:yes:sell",
) -> QuoteIntent:
    return QuoteIntent(
        quote_id=quote_id,
        market_ticker="M1",
        action="sell",
        side="yes",
        yes_price=parse_price_fp(price),
        count=count,
    )


def make_book(
    *,
    bids: tuple[tuple[str, str], ...] = (("0.5000", "1.00"),),
    asks: tuple[tuple[str, str], ...] = (("0.5100", "1.00"),),
) -> Orderbook:
    return Orderbook.from_snapshot(
        market_ticker="M1",
        seq=1,
        bids_raw=bids,
        asks_raw=asks,
        price_ranges=PRICE_RANGES,
    )


def test_live_order_manager_dry_run_replaces_changed_quotes() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        created, canceled = await manager.sync_quotes("M1", [buy_intent()], now=1)
        assert (created, canceled) == (1, 0)
        assert len(manager.orders) == 1

        created, canceled = await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2)
        assert (created, canceled) == (1, 1)
        assert len(manager.orders) == 1

    asyncio.run(run())


def test_live_order_manager_removes_own_quotes_from_strategy_book() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )
        await manager.sync_quotes(
            "M1",
            [buy_intent("0.5000"), sell_intent("0.5100")],
            now=1,
        )
        book = make_book(
            bids=(("0.4900", "2.00"), ("0.5000", "1.00")),
            asks=(("0.5100", "1.00"), ("0.5200", "2.00")),
        )

        external_book = manager.external_orderbook(book)

        assert book.best_bid == parse_price_fp("0.5000")
        assert book.best_ask == parse_price_fp("0.5100")
        assert external_book.best_bid == parse_price_fp("0.4900")
        assert external_book.best_ask == parse_price_fp("0.5200")
        assert external_book.bids[parse_price_fp("0.5000")] == 0
        assert external_book.asks[parse_price_fp("0.5100")] == 0

        strategy = DumbMarketMakerStrategy(count=COUNT_SCALE)
        intents = strategy.on_orderbook(
            context=StrategyContext(event_count=1, offset_seconds=1),
            market_ticker="M1",
            orderbook=external_book,
            portfolio=LivePortfolio(),
        )

        assert [(intent.action, intent.yes_price) for intent in intents] == [
            ("buy", parse_price_fp("0.4900")),
            ("sell", parse_price_fp("0.5200")),
        ]

    asyncio.run(run())


def test_live_order_manager_external_book_clamps_oversized_tracked_order() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )
        await manager.sync_quotes(
            "M1",
            [buy_intent("0.5000", count=2 * COUNT_SCALE)],
            now=1,
        )
        book = make_book(bids=(("0.5000", "1.00"),))

        external_book = manager.external_orderbook(book)

        assert external_book.best_bid is None
        assert external_book.bids[parse_price_fp("0.5000")] == 0

    asyncio.run(run())


def test_live_order_manager_rate_limits_replacement_without_canceling_resting_quote() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=10,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2) == (0, 0)
        assert [order.yes_price for order in manager.orders.values()] == [parse_price_fp("0.5000")]

    asyncio.run(run())


def test_live_order_manager_keeps_matching_quote_inside_requote_interval() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=10,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent()], now=2) == (0, 0)
        assert len(manager.orders) == 1

    asyncio.run(run())


def test_live_order_manager_creates_replacement_after_requote_interval() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=10,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2) == (0, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=11) == (1, 1)

    asyncio.run(run())


def test_live_order_manager_keeps_small_quote_changes_below_threshold() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            requote_price_threshold=parse_price_fp("0.0200"),
            requote_size_threshold_bps=5_000,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2) == (0, 0)
        assert [order.yes_price for order in manager.orders.values()] == [parse_price_fp("0.5000")]

    asyncio.run(run())


def test_live_order_manager_replaces_material_quote_changes() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            requote_price_threshold=parse_price_fp("0.0200"),
            requote_size_threshold_bps=5_000,
            # Isolate the price threshold: with the default rest floor in play,
            # a one-second-old order only moves for twice the threshold.
            min_order_rest_seconds=0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4800")], now=2) == (1, 1)
        assert [order.yes_price for order in manager.orders.values()] == [parse_price_fp("0.4800")]

    asyncio.run(run())


def test_live_order_manager_keeps_small_size_changes_with_zero_price_threshold() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            requote_size_threshold_bps=5_000,
        )

        assert await manager.sync_quotes("M1", [buy_intent(count=COUNT_SCALE)], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent(count=90)], now=2) == (0, 0)
        assert [order.remaining_count for order in manager.orders.values()] == [COUNT_SCALE]

    asyncio.run(run())


def test_live_order_manager_respects_minimum_order_rest_time() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_order_rest_seconds=5.0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=2) == (0, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4900")], now=6) == (1, 1)

    asyncio.run(run())


def test_live_order_manager_bypasses_minimum_rest_for_large_price_moves() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_order_rest_seconds=5.0,
            requote_price_threshold=parse_price_fp("0.0200"),
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4600")], now=2) == (1, 1)

    asyncio.run(run())


def test_live_order_manager_rejects_empty_client_prefix() -> None:
    with pytest.raises(ValueError, match="client_prefix"):
        LiveOrderManager(FakeRest(), client_prefix="-")


def test_live_order_manager_rejects_duplicate_quote_ids_before_canceling() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=True, min_requote_seconds=0)

        await manager.sync_quotes("M1", [buy_intent()], now=1)

        with pytest.raises(ValueError, match="duplicate quote_id"):
            await manager.sync_quotes("M1", [buy_intent("0.4900"), buy_intent("0.4800")], now=2)

        assert len(manager.orders) == 1
        assert rest.canceled == []

    asyncio.run(run())


def test_live_order_manager_counts_confirmed_real_creates_only() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {"orders": []}
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        with pytest.raises(RuntimeError, match="batch create returned"):
            await manager.sync_quotes("M1", [buy_intent()], now=1)

        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_skips_real_create_when_balance_is_exhausted() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.available_balance_cents = 0
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (0, 0)
        assert rest.created == []
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_skips_real_create_when_estimated_cost_exceeds_balance() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.available_balance_cents = 49
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        assert await manager.sync_quotes("M1", [buy_intent("0.5000")], now=1) == (0, 0)
        assert rest.created == []
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_logs_rejected_create_entry_without_order_id() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "code": "invalid order",
                    "details": "post only cross",
                }
            ]
        }
        logs: list[str] = []
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0, status=logs.append)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (0, 0)
        assert manager.orders == {}
        assert any("post only cross" in line for line in logs)

    asyncio.run(run())


def test_live_order_manager_cools_down_rejected_quote_before_retrying() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "code": "invalid_order",
                    "details": "post only cross",
                }
            ]
        }
        manager = LiveOrderManager(
            rest,
            dry_run=False,
            min_requote_seconds=0,
            rejection_cooldown_seconds=1.0,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1.0) == (0, 0)
        assert len(rest.created) == 1

        assert await manager.sync_quotes("M1", [buy_intent()], now=1.5) == (0, 0)
        assert len(rest.created) == 1

        assert await manager.sync_quotes("M1", [buy_intent()], now=2.1) == (0, 0)
        assert len(rest.created) == 2

    asyncio.run(run())


def test_live_order_manager_tracks_created_entries_when_other_create_entries_reject() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "code": "invalid order",
                    "details": "post only cross",
                },
                {
                    "order_id": "live-2",
                    "remaining_count": "1.00",
                },
            ]
        }
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        created, canceled = await manager.sync_quotes(
            "M1",
            [buy_intent(), buy_intent("0.4900", quote_id="M1:adaptive:yes:buy-2")],
            now=1,
        )

        assert (created, canceled) == (1, 0)
        assert [order.order_id for order in manager.orders.values()] == ["live-2"]

    asyncio.run(run())


def test_live_order_manager_sets_live_order_expiration_time() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(
            rest,
            dry_run=False,
            min_requote_seconds=0,
            order_expiration_seconds=30,
        )

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)

        assert isinstance(rest.created[0]["expiration_time"], int)
        assert rest.created[0]["time_in_force"] == "good_till_canceled"

    asyncio.run(run())


def test_live_order_manager_updates_remaining_count_from_user_orders() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        await manager.sync_quotes("M1", [buy_intent()], now=1)
        old_client_order_id = next(iter(manager.orders))

        manager.handle_user_order(
            {
                "type": "user_order",
                "msg": {
                    "order_id": manager.orders[old_client_order_id].order_id,
                    "client_order_id": old_client_order_id,
                    "status": "resting",
                    "remaining_count_fp": "0.50",
                },
            }
        )

        created, canceled = await manager.sync_quotes("M1", [buy_intent()], now=2)

        assert (created, canceled) == (1, 1)
        assert old_client_order_id not in manager.orders

    asyncio.run(run())


def test_live_order_manager_updates_remaining_count_from_fills() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        await manager.sync_quotes("M1", [buy_intent()], now=1)
        order = next(iter(manager.orders.values()))

        fill = OrderFill(
            trade_id="t1",
            order_id=order.order_id,
            market_ticker="M1",
            action="buy",
            side="yes",
            yes_price=parse_price_fp("0.5000"),
            count=COUNT_SCALE // 2,
            post_position=COUNT_SCALE // 2,
            is_taker=False,
        )

        manager.handle_fill(fill)
        assert next(iter(manager.orders.values())).remaining_count == COUNT_SCALE // 2

        manager.handle_fill(fill)
        assert next(iter(manager.orders.values())).remaining_count == COUNT_SCALE // 2

        manager.handle_fill(
            OrderFill(
                trade_id="t2",
                order_id=order.order_id,
                market_ticker="M1",
                action="buy",
                side="yes",
                yes_price=parse_price_fp("0.5000"),
                count=COUNT_SCALE // 2,
                post_position=COUNT_SCALE,
                is_taker=False,
            )
        )

        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_does_not_double_count_fill_after_user_order_update() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        await manager.sync_quotes("M1", [buy_intent()], now=1)
        client_order_id = next(iter(manager.orders))
        order = manager.orders[client_order_id]

        manager.handle_user_order(
            {
                "type": "user_order",
                "msg": {
                    "order_id": order.order_id,
                    "client_order_id": client_order_id,
                    "status": "resting",
                    "fill_count_fp": "0.50",
                    "remaining_count_fp": "0.50",
                },
            }
        )
        manager.handle_fill(
            OrderFill(
                trade_id="t1",
                order_id=order.order_id,
                market_ticker="M1",
                action="buy",
                side="yes",
                yes_price=parse_price_fp("0.5000"),
                count=COUNT_SCALE // 2,
                post_position=COUNT_SCALE // 2,
                is_taker=False,
            )
        )

        assert manager.orders[client_order_id].remaining_count == COUNT_SCALE // 2

    asyncio.run(run())


def test_live_order_manager_drops_expired_user_orders() -> None:
    async def run() -> None:
        manager = LiveOrderManager(
            FakeRest(),
            dry_run=True,
            min_requote_seconds=0,
            # These tests exercise the replacement mechanism itself, so they
            # opt out of the queue-preserving defaults deliberately.
            min_order_rest_seconds=0,
            requote_price_threshold=0,
            requote_size_threshold_bps=0,
        )

        await manager.sync_quotes("M1", [buy_intent()], now=1)
        client_order_id = next(iter(manager.orders))
        order = manager.orders[client_order_id]

        manager.handle_user_order(
            {
                "type": "user_order",
                "msg": {
                    "order_id": order.order_id,
                    "client_order_id": client_order_id,
                    "status": "expired",
                    "remaining_count_fp": "1.00",
                },
            }
        )

        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_does_not_track_fully_filled_create_response() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.create_response = {
            "orders": [
                {
                    "order_id": "live-1",
                    "remaining_count": "0.00",
                }
            ]
        }
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert manager.orders == {}

    asyncio.run(run())


def test_live_order_manager_keeps_order_tracked_when_cancel_response_mismatches() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.cancel_response = {"orders": [{"order_id": "different-order"}]}
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        await manager.sync_quotes("M1", [buy_intent()], now=1)

        with pytest.raises(RuntimeError, match="cancel response missing"):
            await manager.sync_quotes("M1", [], now=2)

        assert len(manager.orders) == 1

    asyncio.run(run())


def test_live_order_manager_matches_create_response_by_client_order_id() -> None:
    async def run() -> None:
        rest = FakeRest()
        rest.reverse_create_response = True
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)

        await manager.sync_quotes(
            "M1",
            [buy_intent(), buy_intent("0.4900", quote_id="M1:adaptive:yes:buy-2")],
            now=1,
        )

        order_ids_by_quote = {
            order.quote_id: order.order_id
            for order in manager.orders.values()
        }

        assert order_ids_by_quote == {
            "M1:adaptive:yes:buy": "live-1",
            "M1:adaptive:yes:buy-2": "live-2",
        }

    asyncio.run(run())


def test_live_order_manager_cancel_all_attempts_remaining_orders_after_failure() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)
        await manager.sync_quotes("M1", [buy_intent(), buy_intent("0.4900", quote_id="M1:adaptive:yes:sell")], now=1)
        first_order_id = next(iter(manager.orders.values())).order_id
        rest.cancel_failures.add(first_order_id)

        with pytest.raises(RuntimeError, match="failed to cancel"):
            await manager.cancel_all()

        assert len(rest.canceled) == 2
        assert len(manager.orders) == 1

    asyncio.run(run())


def test_shutdown_cancel_sweeps_untracked_prefix_orders() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)
        await manager.sync_quotes("M1", [buy_intent()], now=1)
        rest.resting_orders_by_ticker["M1"] = [
            {"order_id": "untracked-1", "client_order_id": f"{manager.client_prefix}-old-1"}
        ]

        canceled = await _cancel_on_shutdown(manager, ("M1",), dry_run=False)

        assert canceled == 2
        assert {payload["order_id"] for payload in rest.canceled} == {
            "live-1",
            "untracked-1",
        }

    asyncio.run(run())


def test_shutdown_cancel_does_not_repeat_recently_canceled_orders() -> None:
    async def run() -> None:
        rest = FakeRest()
        manager = LiveOrderManager(rest, dry_run=False, min_requote_seconds=0)
        await manager.sync_quotes("M1", [buy_intent()], now=1)
        live_order = next(iter(manager.orders.values()))
        rest.resting_orders_by_ticker["M1"] = [
            {
                "order_id": live_order.order_id,
                "client_order_id": live_order.client_order_id,
            }
        ]

        canceled = await _cancel_on_shutdown(manager, ("M1",), dry_run=False)

        assert canceled == 1
        assert [payload["order_id"] for payload in rest.canceled] == [live_order.order_id]

    asyncio.run(run())


def test_live_portfolio_applies_private_position_messages() -> None:
    portfolio = LivePortfolio()
    fill = OrderFill(
        trade_id="t1",
        order_id="o1",
        market_ticker="M1",
        action="buy",
        side="yes",
        yes_price=parse_price_fp("0.5000"),
        count=COUNT_SCALE,
        post_position=COUNT_SCALE,
        is_taker=False,
    )
    position = MarketPosition(
        market_ticker="M1",
        position=2 * COUNT_SCALE,
        position_cost=0,
        realized_pnl=0,
        fees_paid=0,
        volume=0,
    )

    portfolio.apply_message(fill)
    assert portfolio.position("M1") == COUNT_SCALE

    portfolio.apply_message(position)
    assert portfolio.position("M1") == 2 * COUNT_SCALE


def test_default_policy_holds_queue_position_through_a_one_tick_move() -> None:
    """The regression that cost ~2,000 orders for 74 fills.

    With every threshold at zero, a one-tick change counted as material and the
    bot cancelled and re-queued behind thousands of contracts. The defaults must
    now sit still for noise.
    """

    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        # One tick away, one second later: hold the spot.
        assert await manager.sync_quotes("M1", [buy_intent("0.4990")], now=2) == (0, 0)

    asyncio.run(run())


def test_default_policy_still_abandons_position_on_a_real_repricing() -> None:
    """Sticky is not frozen: twice the threshold beats a stale quote."""

    async def run() -> None:
        manager = LiveOrderManager(FakeRest(), dry_run=True)

        assert await manager.sync_quotes("M1", [buy_intent()], now=1) == (1, 0)
        assert await manager.sync_quotes("M1", [buy_intent("0.4700")], now=2) == (1, 1)

    asyncio.run(run())


def test_trader_defaults_do_not_shadow_the_requote_policy() -> None:
    """Passing nothing must inherit the policy's values, not silently zero them."""

    manager = LiveOrderManager(FakeRest(), dry_run=True)

    assert manager.requote_policy == RequotePolicy()
    assert manager.requote_policy.min_order_rest_seconds > 0
    assert manager.requote_policy.price_change_threshold > 0
