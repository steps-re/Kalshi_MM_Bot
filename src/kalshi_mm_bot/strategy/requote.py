from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kalshi_mm_bot.market.types import MarketTicker, OrderAction, OutcomeSide
from kalshi_mm_bot.strategy.types import QuoteIntent

BPS_SCALE = 10_000


class RestingQuote(Protocol):
    quote_id: str
    market_ticker: MarketTicker
    action: OrderAction
    side: OutcomeSide
    yes_price: int
    remaining_count: int


@dataclass(frozen=True, slots=True)
class RequotePolicy:
    """When to give up queue position in exchange for a better price.

    Defaults are deliberately sticky. Every replacement cancels a resting order
    and sends the new one to the back of the queue, and on Kalshi that queue is
    long: 1,400 to 4,100 contracts sit at the touch of a 15-minute crypto
    window. With all four thresholds at zero - the previous defaults - any
    one-tick move counted as material, so the bot re-quoted on essentially
    every book update and never advanced. It placed about 2,000 orders for 74
    fills. The machinery to hold position existed and was switched off.

    A quoted price that is one tick stale costs a tick. Losing queue position
    costs every fill you would have had while you walk back up, which in a
    market that recycles in tens of seconds is much more. So the bar for
    abandoning a spot is a full cent of price movement, and orders get a floor
    of thirty seconds to work before ordinary drift can dislodge them.

    `_forces_requote` remains the escape hatch: a move of twice the threshold
    is a genuine repricing rather than noise, and beats holding a stale quote.
    """

    min_requote_seconds: float = 0.0
    min_order_rest_seconds: float = 30.0
    price_change_threshold: int = 100  # ticks; 100 == one cent
    size_change_threshold_bps: int = 2_500

    def __post_init__(self) -> None:
        if self.min_requote_seconds < 0:
            raise ValueError("min_requote_seconds must be non-negative")
        if self.min_order_rest_seconds < 0:
            raise ValueError("min_order_rest_seconds must be non-negative")
        if self.price_change_threshold < 0:
            raise ValueError("price_change_threshold must be non-negative")
        if self.size_change_threshold_bps < 0:
            raise ValueError("size_change_threshold_bps must be non-negative")


def should_replace_quote(
    order: RestingQuote,
    intent: QuoteIntent,
    *,
    policy: RequotePolicy,
    now: float,
    created_at: float,
) -> bool:
    if not same_quote_contract(order, intent):
        return True

    if quote_matches(order, intent):
        return False

    price_change = abs(intent.yes_price - order.yes_price)
    size_change_bps = _size_change_bps(order.remaining_count, intent.count)
    material_change = (
        _is_material(price_change, policy.price_change_threshold)
        or _is_material(size_change_bps, policy.size_change_threshold_bps)
    )

    if not material_change:
        return False

    if now - created_at < policy.min_order_rest_seconds:
        return _forces_requote(price_change, policy.price_change_threshold)

    return True


def quote_matches(order: RestingQuote, intent: QuoteIntent) -> bool:
    return (
        same_quote_contract(order, intent)
        and order.yes_price == intent.yes_price
        and order.remaining_count == intent.count
    )


def same_quote_contract(order: RestingQuote, intent: QuoteIntent) -> bool:
    return (
        order.quote_id == intent.quote_id
        and order.market_ticker == intent.market_ticker
        and order.action == intent.action
        and order.side == intent.side
    )


def _size_change_bps(current_count: int, target_count: int) -> int:
    denominator = max(current_count, target_count)

    if denominator <= 0:
        return 0

    return abs(target_count - current_count) * BPS_SCALE // denominator


def _is_material(delta: int, threshold: int) -> bool:
    return delta > 0 and delta >= threshold


def _forces_requote(price_delta: int, threshold: int) -> bool:
    return threshold > 0 and price_delta >= threshold * 2
