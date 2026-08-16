from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import (
    COUNT_DECIMALS,
    COUNT_SCALE,
    PRICE_DECIMALS,
    PRICE_SCALE,
    format_count_fp,
)
from kalshi_mm_bot.market.types import MarketTicker
from kalshi_mm_bot.sim.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
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
        fee = self.fee_model.fee_micros(
            yes_price=fill.yes_price,
            count=fill.count,
            is_taker=fill.is_taker,
        )

        self.positions[fill.market_ticker] = self.position(fill.market_ticker) + signed_count
        self.cash[fill.market_ticker] = self.cash_value(fill.market_ticker) + signed_cash - fee
        self.volume[fill.market_ticker] = self.volume.get(fill.market_ticker, 0) + fill.count
        self.fees[fill.market_ticker] = self.fees_value(fill.market_ticker) + fee

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
        """Cash plus what inventory would fetch if unwound right now, net of fees.

        Longs hit the bid, shorts lift the ask, and both pay the exit fee. When
        the relevant side of the book is empty we fall back to mid rather than
        pretending the position is worthless, and the caller can tell how often
        that happened via `unwindable_at_touch`.
        """

        value = self.total_cash()

        for ticker, position in self.positions.items():
            if position == 0:
                continue

            book = orderbooks.get(ticker)
            exit_price = _exit_price(book, position)

            if exit_price is None:
                continue

            value += position * exit_price
            value -= self.fee_model.fee_micros(
                yes_price=exit_price,
                count=abs(position),
                is_taker=True,
            )

        return value

    def unwindable_at_touch(self, orderbooks: dict[MarketTicker, Orderbook]) -> bool:
        """True when every open position has a real touch price to exit into."""

        for ticker, position in self.positions.items():
            if position == 0:
                continue

            book = orderbooks.get(ticker)

            if book is None:
                return False

            if position > 0 and book.best_bid is None:
                return False

            if position < 0 and book.best_ask is None:
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


def _exit_price(book: Orderbook | None, position: int) -> int | None:
    if book is None:
        return None

    touch = book.best_bid if position > 0 else book.best_ask

    return touch if touch is not None else _mid_price(book)
