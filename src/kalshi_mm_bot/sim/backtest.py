from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from kalshi_mm_bot.api.feed_controller import FeedController, ORDERBOOK_CHANNEL
from kalshi_mm_bot.market.clock import MarketClock
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR, PRICE_SCALE
from kalshi_mm_bot.market.series import MidSeries
from kalshi_mm_bot.market.view import TopOfBookRow, top_of_book_rows
from kalshi_mm_bot.recording import (
    RecordedRestClient,
    RecordedWebSocketClient,
    RecordingSessionReader,
)
from kalshi_mm_bot.sim.accounting import SimPortfolio
from kalshi_mm_bot.market.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.sim.fills import FillModel, SimulatedFill
from kalshi_mm_bot.sim.orders import SimulatedOrder
from kalshi_mm_bot.strategy.quotes import quote_intent_map
from kalshi_mm_bot.strategy.requote import (
    RequotePolicy,
    quote_matches,
    should_replace_quote,
)
from kalshi_mm_bot.strategy.types import QuoteIntent, Strategy, StrategyContext


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    """Outcome of a replay.

    `cash_value` and `mark_to_market_value` are net of fees and keep their old
    names so existing callers keep working, but they no longer mean what they
    used to - the pre-fee figures are `gross_cash_value` and
    `gross_mark_to_market_value` if you need to see the difference.
    `net_liquidation_value` is the honest bottom line: fees paid, and leftover
    inventory valued at the touch it could actually be unwound into rather than
    at mid.
    """

    strategy_name: str
    fill_model: str
    event_count: int
    order_count: int
    open_order_count: int
    fill_count: int
    buy_filled_count: int
    sell_filled_count: int
    position_count: int
    volume_count: int
    cash_value: int
    mark_to_market_value: int
    starting_balance_cents: int | None = None
    reserved_risk_cents: int = 0
    skipped_order_count: int = 0
    fees_paid: int = 0
    gross_cash_value: int = 0
    gross_mark_to_market_value: int = 0
    net_liquidation_value: int = 0
    maker_fill_count: int = 0
    taker_fill_count: int = 0
    fee_ceiling_surcharge: int = 0
    unwindable_at_touch: bool = True

    @property
    def inventory_mark_gap(self) -> int:
        """Mid mark minus achievable exit. Non-zero only when ending non-flat."""

        return self.mark_to_market_value - self.net_liquidation_value


@dataclass(frozen=True, slots=True)
class BacktestUpdate:
    event_count: int
    updated_ticker: str | None
    rows: tuple[TopOfBookRow, ...]
    summary: BacktestSummary
    recent_fills: tuple[SimulatedFill, ...]
    final: bool = False


@dataclass(frozen=True, slots=True)
class BacktestResult:
    recording: Path
    tickers: tuple[str, ...]
    summary: BacktestSummary
    fills: tuple[SimulatedFill, ...]
    orders: tuple[SimulatedOrder, ...]
    final_rows: tuple[TopOfBookRow, ...]
    # Mid over time per ticker, retained so markout and equity-curve analysis
    # can run without replaying the recording a second time.
    mid_series: dict[str, MidSeries] = field(default_factory=dict)
    equity_curve: tuple[tuple[float, int], ...] = ()
    # Per-ticker closing inventory. A session spanning several markets
    # cannot be marked with one position and one price.
    positions_by_ticker: dict[str, int] = field(default_factory=dict)
    # Closing mid per ticker, as a number rather than the formatted string in
    # final_rows. P&L attribution needs to value residual inventory, and
    # re-parsing a display row to do it invites exactly the unit confusion this
    # codebase keeps finding. None where the book had no two-sided market at
    # the end, which is normal for a market that has settled.
    final_mids_by_ticker: dict[str, int | None] = field(default_factory=dict)


UpdateCallback = Callable[[BacktestUpdate], None]
StopRequested = Callable[[], bool]


class _SeriesRecorder:
    """Collects the time series post-hoc analytics need.

    Sampling the equity curve on every book event would make the series mostly
    duplicate points and dominate memory on a long recording, so it is thinned
    to a fixed interval. Mid prices are kept at full resolution because markout
    reads them at arbitrary offsets.
    """

    def __init__(self, *, equity_interval_seconds: float = 1.0) -> None:
        self.equity_interval_seconds = equity_interval_seconds
        self._offsets: dict[str, list[float]] = {}
        self._mids: dict[str, list[int]] = {}
        self._equity: list[tuple[float, int]] = []
        self._last_equity_offset: float | None = None

    def observe_book(self, ticker: str, book: Orderbook, offset_seconds: float) -> None:
        if book.best_bid is None or book.best_ask is None:
            return

        self._offsets.setdefault(ticker, []).append(offset_seconds)
        self._mids.setdefault(ticker, []).append((book.best_bid + book.best_ask) // 2)

    def observe_equity(
        self,
        portfolio: SimPortfolio,
        orderbooks: dict[str, Orderbook],
        offset_seconds: float,
    ) -> None:
        due = (
            self._last_equity_offset is None
            or offset_seconds - self._last_equity_offset >= self.equity_interval_seconds
        )

        if not due:
            return

        self._last_equity_offset = offset_seconds
        self._equity.append((offset_seconds, portfolio.mark_to_market(orderbooks)))

    def mid_series(self) -> dict[str, MidSeries]:
        return {
            ticker: MidSeries(
                market_ticker=ticker,
                offsets=tuple(offsets),
                mids=tuple(self._mids[ticker]),
            )
            for ticker, offsets in self._offsets.items()
        }

    def equity_curve(self) -> tuple[tuple[float, int], ...]:
        return tuple(self._equity)


class SimulatedOrderManager:
    def __init__(
        self,
        *,
        fill_model: FillModel,
        portfolio: SimPortfolio,
        latency_seconds: float = 0.0,
        requote_policy: RequotePolicy | None = None,
        starting_balance_cents: int | None = None,
    ) -> None:
        if latency_seconds < 0:
            raise ValueError("latency_seconds must be non-negative")

        self.fill_model = fill_model
        self.portfolio = portfolio
        self.latency_seconds = latency_seconds
        self.requote_policy = requote_policy or RequotePolicy()
        self.starting_balance_cents = starting_balance_cents
        self.orders: dict[str, SimulatedOrder] = {}
        self.fills: list[SimulatedFill] = []
        self.event_count = 0
        self.skipped_order_count = 0

        self._next_order_number = 1
        self._last_sync_by_ticker: dict[str, float] = {}

    def process_market_event(
        self,
        raw_msg: dict,
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> tuple[SimulatedFill, ...]:
        self.event_count = context.event_count
        self._settle_due_orders(orderbooks, context)
        candidates = self.fill_model.process_event(
            raw_msg,
            orderbooks,
            tuple(order for order in self.orders.values() if order.is_fillable),
            context,
        )
        fills: list[SimulatedFill] = []

        for candidate in candidates:
            order = self.orders.get(candidate.order_id)

            if order is None or not order.is_fillable:
                continue

            count = min(candidate.count, order.remaining_count)

            if count <= 0:
                continue

            fill = candidate if count == candidate.count else replace(candidate, count=count)
            order.remaining_count -= count
            self.portfolio.apply_fill(fill)
            self.fills.append(fill)
            fills.append(fill)

            if order.remaining_count <= 0:
                order.status = "filled"
                self.fill_model.on_order_closed(order)

        return tuple(fills)

    def sync_market_quotes(
        self,
        market_ticker: str,
        intents: Iterable[QuoteIntent],
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> None:
        wanted = quote_intent_map(intents)
        self._settle_due_orders(orderbooks, context)
        last_sync = self._last_sync_by_ticker.get(market_ticker)
        can_place = (
            last_sync is None
            or context.offset_seconds - last_sync >= self.requote_policy.min_requote_seconds
        )
        replacements: list[SimulatedOrder] = []

        for order in tuple(self.orders.values()):
            if order.market_ticker != market_ticker or not _is_live_order(order):
                continue

            intent = wanted.get(order.quote_id)

            if intent is None:
                self.cancel_order(order, context)
                continue

            if should_replace_quote(
                order,
                intent,
                policy=self.requote_policy,
                now=context.offset_seconds,
                created_at=order.created_offset_seconds,
            ):
                replacements.append(order)

        if can_place:
            for order in replacements:
                self.cancel_order(order, context)

        if not can_place:
            return

        placed = False

        for intent in wanted.values():
            if self._has_matching_live_order(intent):
                continue

            if self.place_order(intent, orderbooks, context) is not None:
                placed = True

        if placed:
            self._last_sync_by_ticker[market_ticker] = context.offset_seconds

    def place_order(
        self,
        intent: QuoteIntent,
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> SimulatedOrder | None:
        book = orderbooks.get(intent.market_ticker)

        if book is None:
            return None

        if self._exceeds_balance(intent):
            self.skipped_order_count += 1
            return None

        order = SimulatedOrder.from_intent(
            self._next_order_id(),
            intent,
            now_offset_seconds=context.offset_seconds,
            latency_seconds=self.latency_seconds,
        )
        self.orders[order.order_id] = order
        self._open_if_due(order, book, context)
        return order

    def cancel_order(self, order: SimulatedOrder, context: StrategyContext) -> None:
        if order.status in {"canceled", "filled"}:
            return

        if order.status == "pending_open":
            order.status = "canceled"
            order.canceled_offset_seconds = context.offset_seconds
            return

        cancel_offset = context.offset_seconds + self.latency_seconds

        if cancel_offset <= context.offset_seconds:
            order.status = "canceled"
            order.canceled_offset_seconds = context.offset_seconds
            self.fill_model.on_order_closed(order)
            return

        order.status = "pending_cancel"
        order.canceled_offset_seconds = cancel_offset

    def _settle_due_orders(
        self,
        orderbooks: dict[str, Orderbook],
        context: StrategyContext,
    ) -> None:
        for order in tuple(self.orders.values()):
            if order.status == "pending_open":
                book = orderbooks.get(order.market_ticker)

                if book is not None:
                    self._open_if_due(order, book, context)

            elif (
                order.status == "pending_cancel"
                and order.canceled_offset_seconds is not None
                and order.canceled_offset_seconds <= context.offset_seconds
            ):
                order.status = "canceled"
                self.fill_model.on_order_closed(order)

    def _open_if_due(
        self,
        order: SimulatedOrder,
        book: Orderbook,
        context: StrategyContext,
    ) -> None:
        if order.status != "pending_open":
            return

        if order.active_offset_seconds > context.offset_seconds:
            return

        order.status = "open"
        self.fill_model.on_order_opened(order, book)

    def _has_matching_live_order(self, intent: QuoteIntent) -> bool:
        return any(
            _is_live_order(order)
            and order.status != "pending_cancel"
            and _matches_intent(order, intent)
            for order in self.orders.values()
        )

    def _next_order_id(self) -> str:
        order_id = f"sim-{self._next_order_number}"
        self._next_order_number += 1
        return order_id

    def reserved_risk_cents(self) -> int:
        return sum(
            _estimated_required_cents(order)
            for order in self.orders.values()
            if _is_live_order(order)
        )

    def _exceeds_balance(self, intent: QuoteIntent) -> bool:
        if self.starting_balance_cents is None:
            return False

        return (
            self.reserved_risk_cents() + _estimated_required_cents(intent)
            > self._available_balance_cents()
        )

    def _available_balance_cents(self) -> int:
        if self.starting_balance_cents is None:
            return 0

        cash_cents = self.portfolio.total_cash() * 100 // (PRICE_SCALE * COUNT_SCALE)
        return max(0, self.starting_balance_cents + cash_cents)


async def run_replay_backtest(
    recording: str | Path,
    *,
    strategy: Strategy,
    fill_model: FillModel,
    speed_multiplier: float = 0.0,
    latency_seconds: float = 0.0,
    requote_policy: RequotePolicy | None = None,
    starting_balance_cents: int | None = None,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    on_update: UpdateCallback | None = None,
    update_interval_seconds: float = 0.25,
    stop_requested: StopRequested | None = None,
) -> BacktestResult:
    reader = RecordingSessionReader.open(recording)

    if ORDERBOOK_CHANNEL not in reader.manifest.channels:
        raise ValueError("backtests require orderbook_delta recordings")

    ws = RecordedWebSocketClient.from_session(reader, speed_multiplier=speed_multiplier)
    rest = RecordedRestClient(reader.manifest)
    controller = FeedController(rest=rest, ws=ws)
    # Older recordings predate close-time capture; the clock is then empty and
    # every strategy sees seconds_to_close=None, which is the correct answer.
    market_clock = MarketClock.from_iso_map(reader.manifest.metadata.get("close_times_utc"))
    portfolio = SimPortfolio(fee_model=fee_model)
    series = _SeriesRecorder()
    manager = SimulatedOrderManager(
        fill_model=fill_model,
        portfolio=portfolio,
        latency_seconds=latency_seconds,
        requote_policy=requote_policy,
        starting_balance_cents=starting_balance_cents,
    )
    last_update_monotonic = 0.0
    result: BacktestResult | None = None

    try:
        await controller.connect()
        await controller.subscribe(reader.manifest.tickers, channels=(ORDERBOOK_CHANNEL,))

        while True:
            if stop_requested is not None and stop_requested():
                break

            try:
                updated_ticker = await controller.recv()
            except EOFError:
                break

            event = ws.last_event

            if event is None:
                continue

            context = StrategyContext(
                event_count=ws.returned_count,
                offset_seconds=event.offset_seconds,
                observed_at_utc=event.observed_at_utc,
                seconds_to_close=(
                    market_clock.seconds_to_close(
                        updated_ticker,
                        now_utc=event.observed_at_utc,
                    )
                    if updated_ticker is not None
                    else None
                ),
            )
            recent_fills = manager.process_market_event(event.msg, controller.orderbooks, context)
            series.observe_equity(portfolio, controller.orderbooks, event.offset_seconds)

            if updated_ticker is not None:
                book = controller.orderbooks.get(updated_ticker)

                if book is not None:
                    series.observe_book(updated_ticker, book, event.offset_seconds)
                    intents = strategy.on_orderbook(context, updated_ticker, book, portfolio)
                    manager.sync_market_quotes(
                        updated_ticker,
                        intents,
                        controller.orderbooks,
                        context,
                    )

            if on_update is not None:
                now = time.monotonic()

                if now - last_update_monotonic >= update_interval_seconds:
                    last_update_monotonic = now
                    on_update(
                        _build_update(
                            reader,
                            strategy,
                            fill_model,
                            manager,
                            controller,
                            recent_fills,
                            updated_ticker=updated_ticker,
                        )
                    )

        result = _build_result(reader, strategy, fill_model, manager, controller, series)
    finally:
        await controller.close()

    if result is None:
        raise RuntimeError("backtest ended before a result could be built")

    if on_update is not None:
        on_update(
            BacktestUpdate(
                event_count=result.summary.event_count,
                updated_ticker=None,
                rows=result.final_rows,
                summary=result.summary,
                recent_fills=(),
                final=True,
            )
        )

    return result


def _build_update(
    reader: RecordingSessionReader,
    strategy: Strategy,
    fill_model: FillModel,
    manager: SimulatedOrderManager,
    controller: FeedController,
    recent_fills: tuple[SimulatedFill, ...],
    updated_ticker: str | None,
) -> BacktestUpdate:
    summary = _build_summary(strategy, fill_model, manager, controller)
    return BacktestUpdate(
        event_count=summary.event_count,
        updated_ticker=updated_ticker,
        rows=top_of_book_rows(controller.orderbooks, reader.manifest.tickers),
        summary=summary,
        recent_fills=recent_fills,
    )


def _build_result(
    reader: RecordingSessionReader,
    strategy: Strategy,
    fill_model: FillModel,
    manager: SimulatedOrderManager,
    controller: FeedController,
    series: _SeriesRecorder,
) -> BacktestResult:
    return BacktestResult(
        recording=reader.directory,
        tickers=reader.manifest.tickers,
        summary=_build_summary(strategy, fill_model, manager, controller),
        fills=tuple(manager.fills),
        orders=tuple(manager.orders.values()),
        final_rows=top_of_book_rows(controller.orderbooks, reader.manifest.tickers),
        mid_series=series.mid_series(),
        equity_curve=series.equity_curve(),
        positions_by_ticker=dict(manager.portfolio.positions),
        final_mids_by_ticker={
            ticker: _book_mid(controller.orderbooks.get(ticker))
            for ticker in reader.manifest.tickers
        },
    )


def _book_mid(book) -> int | None:
    """Mid of a book, or None when it is not two-sided."""

    if book is None:
        return None

    best_bid, best_ask = book.best_bid, book.best_ask

    if best_bid is None or best_ask is None or best_bid >= best_ask:
        return None

    return (best_bid + best_ask) // 2


def _build_summary(
    strategy: Strategy,
    fill_model: FillModel,
    manager: SimulatedOrderManager,
    controller: FeedController,
) -> BacktestSummary:
    fills = tuple(manager.fills)
    portfolio = manager.portfolio
    books = controller.orderbooks
    fee_model = portfolio.fee_model
    gross_cash = portfolio.gross_cash()
    mark_to_market = portfolio.mark_to_market(books)
    fees_paid = portfolio.total_fees()

    return BacktestSummary(
        strategy_name=strategy.name,
        fill_model=fill_model.name,
        event_count=manager.event_count,
        order_count=len(manager.orders),
        open_order_count=sum(1 for order in manager.orders.values() if order.is_fillable),
        fill_count=len(fills),
        buy_filled_count=sum(fill.count for fill in fills if fill.action == "buy"),
        sell_filled_count=sum(fill.count for fill in fills if fill.action == "sell"),
        position_count=portfolio.total_position_count(),
        volume_count=portfolio.total_volume_count(),
        cash_value=portfolio.total_cash(),
        mark_to_market_value=mark_to_market,
        starting_balance_cents=manager.starting_balance_cents,
        reserved_risk_cents=manager.reserved_risk_cents(),
        skipped_order_count=manager.skipped_order_count,
        fees_paid=fees_paid,
        gross_cash_value=gross_cash,
        gross_mark_to_market_value=mark_to_market + fees_paid,
        net_liquidation_value=portfolio.liquidation_value(books),
        maker_fill_count=sum(1 for fill in fills if not fill.is_taker),
        taker_fill_count=sum(1 for fill in fills if fill.is_taker),
        fee_ceiling_surcharge=sum(
            fee_model.ceiling_surcharge_micros(yes_price=fill.yes_price, count=fill.count)
            for fill in fills
        ),
        unwindable_at_touch=portfolio.unwindable_at_touch(books),
    )


def _is_live_order(order: SimulatedOrder) -> bool:
    return order.status in {"pending_open", "open", "pending_cancel"}


def _matches_intent(order: SimulatedOrder, intent: QuoteIntent) -> bool:
    return quote_matches(order, intent)


def _estimated_required_cents(intent: QuoteIntent | SimulatedOrder) -> int:
    risk_price = intent.yes_price if intent.action == "buy" else ONE_DOLLAR - intent.yes_price
    count = intent.remaining_count if isinstance(intent, SimulatedOrder) else intent.count
    return _ceil_div(risk_price * count * 100, PRICE_SCALE * COUNT_SCALE)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)
