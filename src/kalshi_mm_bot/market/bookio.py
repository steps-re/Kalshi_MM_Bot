"""Read Kalshi orderbooks from either feed, with the side conventions in one place.

Kalshi encodes the ask side differently depending on where you read it, and
that difference produced at least three broken analyses in one day:

* **REST** `GET /markets/{t}/orderbook` returns `orderbook_fp` whose
  `no_dollars` list is quoted in **NO prices**. A resting NO bid at price q is a
  YES ask at 1 - q, so the ask ladder is recovered by folding every price about
  a dollar.
* **The websocket feed** (and recordings made from it) carries
  `yes_dollars_fp` / `no_dollars_fp` where the `no` list is **already in YES
  price space** - `["0.4500", ...]` there IS a YES ask at 45c, and folding it
  produces a permanently crossed book.

Each convention is obvious once stated and neither is guessable from the field
names, which are nearly identical. A spot-versus-Kalshi sampler returned zero
samples twice (folded the REST book that... actually needed folding, but read
the empty result as "no data" instead of a convention error), and a mid-path
reconstruction from recordings folded the websocket book and self-crossed
within thirty seconds, silently truncating every window to its first bucket.

The rule: **name the feed you are reading and call its function.** Anything
that parses `no_dollars` inline is a future copy of one of those bugs.

Prices are returned in ticks ($1.0000 == 10_000) and sizes in COUNT fixed
point (1.00 contract == 100), matching the rest of the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_mm_bot.market.price import ONE_DOLLAR, parse_price_fp


@dataclass(frozen=True, slots=True)
class TopOfBook:
    """Best bid and ask in YES space, with the sizes resting there."""

    bid: int
    ask: int
    bid_size: float
    ask_size: float

    @property
    def mid(self) -> int:
        return (self.bid + self.ask) // 2

    @property
    def spread_ticks(self) -> int:
        return self.ask - self.bid


def _levels(raw: object) -> dict[int, float]:
    levels: dict[int, float] = {}

    for entry in raw or ():  # type: ignore[union-attr]
        try:
            price, size = entry[0], entry[1]
            levels[parse_price_fp(str(price))] = float(size)
        except (TypeError, ValueError, IndexError):
            continue

    return levels


def rest_book(orderbook_fp: dict) -> tuple[dict[int, float], dict[int, float]]:
    """Bid and ask ladders from a REST `orderbook_fp` payload.

    The `no_dollars` list is in NO prices and is folded about a dollar here,
    once, so no caller ever does it again.
    """

    bids = _levels(orderbook_fp.get("yes_dollars"))
    asks = {
        ONE_DOLLAR - price: size
        for price, size in _levels(orderbook_fp.get("no_dollars")).items()
    }
    return bids, asks


def ws_book(message: dict) -> tuple[dict[int, float], dict[int, float]]:
    """Bid and ask ladders from a websocket snapshot's inner message.

    `no_dollars_fp` is already in YES price space. Folding it is the bug.
    """

    bids = _levels(message.get("yes_dollars_fp"))
    asks = _levels(message.get("no_dollars_fp"))
    return bids, asks


def top_of_book(
    bids: dict[int, float], asks: dict[int, float]
) -> TopOfBook | None:
    """Best levels, or None when the book is empty, one-sided or crossed.

    A crossed result almost always means the wrong convention was applied to
    the feed being read; callers should treat repeated None as a red flag, not
    as a quiet market.
    """

    if not bids or not asks:
        return None

    bid = max(bids)
    ask = min(asks)

    if bid >= ask:
        return None

    return TopOfBook(bid=bid, ask=ask, bid_size=bids[bid], ask_size=asks[ask])


def rest_top(orderbook_fp: dict) -> TopOfBook | None:
    return top_of_book(*rest_book(orderbook_fp))


def ws_top(message: dict) -> TopOfBook | None:
    return top_of_book(*ws_book(message))
