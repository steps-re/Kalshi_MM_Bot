from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import (
    COUNT_DECIMALS,
    COUNT_SCALE,
    ONE_DOLLAR,
    PRICE_DECIMALS,
    PRICE_SCALE,
    format_count_fp,
)
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.market.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.sim.fills import SimulatedFill


CASH_SCALE = PRICE_SCALE * COUNT_SCALE
CASH_DECIMALS = PRICE_DECIMALS + COUNT_DECIMALS


@dataclass(slots=True)
class SimPortfolio:
    """Cash, inventory and fees for a replayed session.

    `cash` is net of fees. Two marks are exposed because they answer different
    questions: `mark_to_market` values inventory at mid, which is the number
    every naive backtest prints, and `liquidation_value` values it at the price
    you could actually get out at, net of the fees getting out would cost. A
    market maker that ends flat sees no difference; one that ends holding
    inventory sees the gap, and the gap is the part that turns a profitable
    backtest into a losing session.
    """

    positions: dict[MarketTicker, int] = field(default_factory=dict)
    cash: dict[MarketTicker, int] = field(default_factory=dict)
    volume: dict[MarketTicker, int] = field(default_factory=dict)
    fees: dict[MarketTicker, int] = field(default_factory=dict)
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL
    # Contracts filled so far per order, so the per-order fee ceiling is
    # applied to the order rather than to each partial fill.
    _filled_by_order: dict[str, int] = field(default_factory=dict, init=False)

    def position(self, market_ticker: MarketTicker) -> int:
        return self.positions.get(market_ticker, 0)

    def cash_value(self, market_ticker: MarketTicker) -> int:
        return self.cash.get(market_ticker, 0)

    def fees_value(self, market_ticker: MarketTicker) -> int:
        return self.fees.get(market_ticker, 0)

    def apply_fill(self, fill: SimulatedFill) -> None:
        direction = 1 if fill.action == "buy" else -1
        signed_count = direction * fill.count
        signed_cash = -direction * fill.yes_price * fill.count
        fee = self._incremental_fee(fill)

        self.positions[fill.market_ticker] = self.position(fill.market_ticker) + signed_count
        self.cash[fill.market_ticker] = self.cash_value(fill.market_ticker) + signed_cash - fee
        self.volume[fill.market_ticker] = self.volume.get(fill.market_ticker, 0) + fill.count
        self.fees[fill.market_ticker] = self.fees_value(fill.market_ticker) + fee

    def _incremental_fee(self, fill: SimulatedFill) -> int:
        """Fee this fill adds, given what its order has already been charged.

        Kalshi rounds an order's fee up to the cent once. Charging the ceiling
        on every partial fill would bill a ten-contract order that filled in
        ten pieces ten separate roundings, which at the small sizes this bot
        trades is a large fabricated cost. Instead, recompute the whole order's
        fee at its new cumulative size and charge only the difference.
        """

        already_filled = self._filled_by_order.get(fill.order_id, 0)
        total_filled = already_filled + fill.count
        self._filled_by_order[fill.order_id] = total_filled

        charged_so_far = (
            self.fee_model.fee_micros(
                yes_price=fill.yes_price,
                count=already_filled,
                is_taker=fill.is_taker,
            )
            if already_filled
            else 0
        )
        charged_total = self.fee_model.fee_micros(
            yes_price=fill.yes_price,
            count=total_filled,
            is_taker=fill.is_taker,
        )

        return max(0, charged_total - charged_so_far)

    def total_cash(self) -> int:
        return sum(self.cash.values())

    def total_fees(self) -> int:
        return sum(self.fees.values())

    def gross_cash(self) -> int:
        """Cash before fees - what the simulator reported prior to this change."""

        return self.total_cash() + self.total_fees()

    def total_position_count(self) -> int:
        return sum(self.positions.values())

    def total_volume_count(self) -> int:
        return sum(self.volume.values())

    def mark_to_market(self, orderbooks: dict[MarketTicker, Orderbook]) -> int:
        value = self.total_cash()

        for ticker, position in self.positions.items():
            book = orderbooks.get(ticker)
            mid = _mid_price(book)

            if mid is not None:
                value += position * mid

        return value

    def liquidation_value(self, orderbooks: dict[MarketTicker, Orderbook]) -> int:
        """Cash plus what inventory would actually fetch if unwound right now.

        Walks the book rather than marking the whole position at the touch: a
        hundred contracts against one contract on the bid do not all get the
        bid. Longs sell down the bid side, shorts buy up the ask side, and both
        pay the exit fee.

        Anything that cannot be filled by the visible book is marked at the
        worst case - zero for a long, a dollar for a short - because that is
        the bound, not because it is likely. Silently omitting it, which is the
        tempting alternative, removes a short's liability entirely and reports
        an account as richer than it is.
        """

        value = self.total_cash()

        for ticker, position in self.positions.items():
            if position == 0:
                continue

            proceeds, filled = _walk_book_exit(orderbooks.get(ticker), position)
            value += proceeds

            unfilled = abs(position) - filled

            if unfilled > 0:
                # A long we cannot sell is worth nothing; a short we cannot
                # buy back settles at a dollar against us.
                value -= unfilled * ONE_DOLLAR if position < 0 else 0

            if filled:
                value -= self.fee_model.fee_micros(
                    yes_price=_average_exit_price(proceeds, filled),
                    count=filled,
                    is_taker=True,
                )

        return value

    def unwindable_at_touch(self, orderbooks: dict[MarketTicker, Orderbook]) -> bool:
        """True when the visible book could absorb every open position."""

        for ticker, position in self.positions.items():
            if position == 0:
                continue

            _, filled = _walk_book_exit(orderbooks.get(ticker), position)

            if filled < abs(position):
                return False

        return True


def format_money_value(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    whole = value // CASH_SCALE
    frac = value % CASH_SCALE
    return f"{sign}{whole}.{frac:0{CASH_DECIMALS}d}"


def format_contract_count(count: int) -> str:
    return format_count_fp(count)


def _mid_price(book: Orderbook | None) -> int | None:
    if book is None or book.best_bid is None or book.best_ask is None:
        return None

    return (book.best_bid + book.best_ask) // 2


def _walk_book_exit(book: Orderbook | None, position: int) -> tuple[int, int]:
    """Consume the book to unwind `position`.

    Returns `(signed_proceeds, contracts_filled)`. Proceeds are positive when
    selling a long and negative when buying back a short, so the caller can add
    them to cash directly.
    """

    if book is None or position == 0:
        return 0, 0

    remaining = abs(position)
    proceeds = 0
    filled = 0

    if position > 0:
        levels = book.bids
        prices = reversed(book.price_levels)  # sell into the highest bids first
        sign = 1
    else:
        levels = book.asks
        prices = iter(book.price_levels)  # buy back from the lowest asks first
        sign = -1

    for price in prices:
        if remaining <= 0:
            break

        available = levels[price]

        if available <= 0:
            continue

        taken = min(available, remaining)
        proceeds += sign * taken * price
        filled += taken
        remaining -= taken

    return proceeds, filled


def _average_exit_price(proceeds: int, filled: int) -> int:
    if filled <= 0:
        return 0

    return abs(proceeds) // filled
