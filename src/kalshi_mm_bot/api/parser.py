from collections.abc import Callable
from datetime import datetime
from typing import Any

from kalshi_mm_bot.market.price import parse_count_fp, parse_money_fp, parse_price_fp
from kalshi_mm_bot.market.types import MarketPosition, OrderFill, PriceRange


ParsedWsMessage = OrderFill | MarketPosition


def parse_ws_message(msg: dict[str, Any]) -> ParsedWsMessage | None:
    msg_type = msg.get("type")

    if msg_type == "fill":
        return parse_order_fill(msg)

    if msg_type == "market_position":
        return parse_market_position(msg)

    return None


def parse_price_ranges(raw_market: dict[str, Any]) -> tuple[PriceRange, ...]:
    return tuple(
        PriceRange(
            start=_parse_field(parse_price_fp, price_range, "start"),
            end=_parse_field(parse_price_fp, price_range, "end"),
            step=_parse_field(parse_price_fp, price_range, "step"),
        )
        for price_range in raw_market["price_ranges"]
    )


def parse_order_fill(msg: dict[str, Any]) -> OrderFill:
    data = msg["msg"]

    return OrderFill(
        trade_id=data["trade_id"],
        order_id=data["order_id"],
        market_ticker=data["market_ticker"],
        is_taker=data["is_taker"],
        side=data["side"],
        action=data["action"],
        yes_price=_parse_field(parse_price_fp, data, "yes_price_dollars"),
        count=_parse_field(parse_count_fp, data, "count_fp"),
        post_position=_parse_field(parse_count_fp, data, "post_position_fp"),
        exchange_ts=parse_exchange_ts(data),
    )


# The venue's own execution stamp. Spelling varies by payload and Kalshi has
# more than one, so try them all and return None rather than guessing: a
# missing stamp must stay missing. Our journal stamps events when we WRITE
# them, so without this the gap between execution and journalling is
# unmeasurable, and every offline join that says "the book just before the
# fill" is really saying "the book just before we heard about the fill".
EXCHANGE_TS_FIELDS = ("ts", "created_time", "executed_time", "trade_time")


def parse_exchange_ts(data: dict[str, Any]) -> float | None:
    for field in EXCHANGE_TS_FIELDS:
        raw = data.get(field)

        if raw is None:
            continue

        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            # Kalshi sends seconds on some channels and milliseconds on others.
            # Anything past the year 2100 in seconds is milliseconds.
            return value / 1000.0 if value > 4_102_444_800 else value

        try:
            return datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            continue

    return None


def parse_market_position(msg: dict[str, Any]) -> MarketPosition:
    data = msg["msg"]

    return MarketPosition(
        market_ticker=data["market_ticker"],
        position=_parse_field(parse_count_fp, data, "position_fp"),
        position_cost=_parse_field(parse_money_fp, data, "position_cost_dollars"),
        realized_pnl=_parse_field(parse_money_fp, data, "realized_pnl_dollars"),
        fees_paid=_parse_field(parse_money_fp, data, "fees_paid_dollars"),
        volume=_parse_field(parse_count_fp, data, "volume_fp"),
    )


# Kalshi has renamed this field; REST /portfolio/fills currently returns
# `fee_cost`. Older payloads and the websocket use the `*_dollars` spellings.
FEE_FIELDS = ("fee_cost", "fees_paid_dollars", "fee_paid_dollars")


def parse_fill_fee_micros(fill: dict[str, Any]) -> int | None:
    """Fee charged on one REST fill, or None when the payload does not say.

    Returns None rather than zero on purpose, and callers must keep the
    distinction. Both scripts that measure fees used to read a field name that
    no longer exists with a `"0"` default, so every fill on the ledger came back
    free - including takers, who are demonstrably charged about 1.3c. That bug
    reported exactly the result the project wanted to be true, which is why it
    survived a whole session of looking straight at it.

    An unknown fee is a measurement failure. Silently calling it zero turns a
    broken reader into evidence for the most profitable hypothesis available.
    """

    for field in FEE_FIELDS:
        value = fill.get(field)

        if value in (None, ""):
            continue

        try:
            return parse_money_fp(str(value))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field}={value!r}: {error}") from error

    return None


def _parse_field(
    parser: Callable[[str], int],
    data: dict[str, Any],
    field: str,
) -> int:
    value = data[field]

    try:
        return parser(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field}={value!r}: {error}") from error
