from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.types import MarketTicker, OrderAction, OutcomeSide


@dataclass(frozen=True, slots=True)
class QuoteIntent:
    quote_id: str
    market_ticker: MarketTicker
    action: OrderAction
    side: OutcomeSide
    yes_price: int
    count: int


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Per-event context handed to a strategy.

    `seconds_to_close` is None whenever the close time is unknown - an older
    recording, or a market whose metadata we could not fetch. Strategies must
    treat None as "no deadline" and keep working, never as "closing now".
    """

    event_count: int
    offset_seconds: float
    observed_at_utc: str | None = None
    seconds_to_close: float | None = None


class PortfolioView(Protocol):
    def position(self, market_ticker: MarketTicker) -> int:
        ...


class Strategy(Protocol):
    name: str

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        ...


StrategyMetadata = dict[str, Any]
