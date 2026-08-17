"""A durable record of what happened to every live order.

The live order manager tracks orders in memory and drops them once filled. That
is correct for trading and useless for knowing whether the trading worked: the
moment a fill lands, every fact about how it got there - what the book looked
like, how long it rested, how much was ahead of it - is gone.

The consequence is not theoretical. `live/campaign.py` halts a campaign when it
cannot measure adverse selection, and adverse selection needs the mid at fill
time. Nothing in the live path recorded it, so the monitor halts every live run
after its grace period, correctly and permanently.

This is the missing half. It records three moments per order:

* **placed** - the book we joined, and the depth ahead of us at our price. The
  depth is the number that decides whether the order was ever going to fill, and
  it is unrecoverable after the fact.
* **filled** - the exchange's own timestamp and the mid at the moment we learned
  of it, with the lag between them recorded rather than assumed. A markout whose
  horizon is unknown cannot be compared to any other markout.
* **cancelled** - so the denominator of a fill rate is real. Counting fills
  without counting the orders that never filled measures nothing.

Written as JSONL, one event per line, flushed on write. A journal that loses the
last few minutes to a buffer when the process dies is a journal that is missing
exactly the part you wanted to read.

Deliberately append-only and free of interpretation: no P&L, no markout, no
verdicts. Those belong in analytics, computed from this, where they can be
recomputed when the method changes - and the method has changed repeatedly.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_mm_bot.market.orderbook import Orderbook


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def book_mid(book: Orderbook | None) -> int | None:
    """Mid of a two-sided book, else None.

    None rather than a one-sided guess: marking against a book that has only a
    bid means marking against our own optimism.
    """

    if book is None:
        return None

    best_bid, best_ask = book.best_bid, book.best_ask

    if best_bid is None or best_ask is None or best_bid >= best_ask:
        return None

    return (best_bid + best_ask) // 2


def depth_ahead(book: Orderbook | None, *, yes_price: int, is_buy: bool) -> float | None:
    """Size resting at our price when we joined it, in COUNT fixed point.

    **Fixed point, not contracts**: 1.00 contract is 100, matching the book and
    everything else internal. Spelled out because unit confusion between the
    API's decimal strings and this scale has produced several wrong numbers in
    this project, and a queue depth that is silently 100x off looks entirely
    plausible.

    This is queue position at placement, and it is the single best predictor of
    whether a resting order fills. It cannot be reconstructed later: by the time
    the order fills or is cancelled, the level has churned.
    """

    if book is None:
        return None

    levels = book.bids if is_buy else book.asks

    try:
        return float(levels[yes_price])
    except (KeyError, TypeError, ValueError):
        return None


@dataclass
class OrderJournal:
    """Append-only JSONL record of order lifecycle events.

    `sink` exists so tests and callers that want events in memory do not have to
    touch a filesystem; when a path is given, events are written and flushed
    immediately.
    """

    path: Path | None = None
    sink: Callable[[dict], None] | None = None
    _handle: Any = field(default=None, init=False, repr=False)
    events: list[dict] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None

    def _write(self, event: dict) -> None:
        event["at"] = _now_iso()
        self.events.append(event)

        if self.sink is not None:
            self.sink(event)

        if self._handle is not None:
            self._handle.write(json.dumps(event) + "\n")
            # Flushed per event on purpose: the interesting minutes are usually
            # the last ones before something went wrong.
            self._handle.flush()

    def record_placed(
        self,
        *,
        order_id: str,
        market_ticker: str,
        action: str,
        yes_price: int,
        count: int,
        book: Orderbook | None,
    ) -> None:
        self._write(
            {
                "event": "placed",
                "order_id": order_id,
                "market_ticker": market_ticker,
                "action": action,
                "yes_price": yes_price,
                "count": count,
                "mid": book_mid(book),
                "depth_ahead": depth_ahead(
                    book, yes_price=yes_price, is_buy=action == "buy"
                ),
            }
        )

    def record_filled(
        self,
        *,
        order_id: str,
        trade_id: str,
        market_ticker: str,
        action: str,
        yes_price: int,
        count: int,
        is_taker: bool,
        fee_micros: int | None,
        book: Orderbook | None,
        executed_at: float | None = None,
    ) -> None:
        """Record a fill. `fee_micros` of None means the payload did not say.

        The websocket fill message does not carry a fee at all, so a live run
        journals None for every fill and the campaign monitor will correctly
        refuse to confirm the maker-fee premise from it. That is the right
        behaviour and not a substitute for the fee: reconcile against
        /portfolio/fills, which does carry `fee_cost`, before drawing any
        conclusion about what a session cost. Measured on a 28-fill live run:
        the journal read 0 of 28 fees while the ledger showed all 28 as maker
        fills charged $0.000000.

        None is not zero and must survive as None all the way to analysis: a fee
        we could not read once made 48 taker fills that really cost $0.5879 look
        free, and confirmed the conclusion the project most wanted to be true.
        """

        mid = book_mid(book)
        lag = None

        if executed_at is not None:
            lag = max(0.0, datetime.now(UTC).timestamp() - executed_at)

        self._write(
            {
                "event": "filled",
                "order_id": order_id,
                "trade_id": trade_id,
                "market_ticker": market_ticker,
                "action": action,
                "yes_price": yes_price,
                "count": count,
                "is_taker": is_taker,
                "fee_micros": fee_micros,
                "mid_at_fill": mid,
                # The markout horizon. Reported, never assumed - the book is
                # sampled when we learn of the fill, not when it happened.
                "mid_lag_seconds": lag,
            }
        )

    def record_cancelled(
        self, *, order_id: str, market_ticker: str, reason: str = ""
    ) -> None:
        """Cancellations are the denominator of a fill rate."""

        self._write(
            {
                "event": "cancelled",
                "order_id": order_id,
                "market_ticker": market_ticker,
                "reason": reason,
            }
        )


def read_journal(path: Path) -> list[dict]:
    """Load a journal, skipping the partial last line of a live writer."""

    events: list[dict] = []

    for line in path.open(encoding="utf-8"):
        try:
            events.append(json.loads(line))
        except ValueError:
            continue

    return events


def fills_for_monitor(events: list[dict]) -> list:
    """Journal events as `campaign.Fill`, ready for the premise monitor.

    This is the join that was missing: the monitor needs a mid at fill time and
    a fee it can trust, and the journal is the only place both exist together.
    """

    from kalshi_mm_bot.live.campaign import Fill

    return [
        Fill(
            yes_price=int(event["yes_price"]),
            count=int(event["count"]),
            is_taker=bool(event.get("is_taker")),
            fee_micros=event.get("fee_micros"),
            mid_at_fill=event.get("mid_at_fill"),
            action=str(event.get("action") or "buy"),
        )
        for event in events
        if event.get("event") == "filled"
    ]


def quote_count(events: list[dict]) -> int:
    """Orders placed, for the fill-rate tripwire."""

    return sum(1 for event in events if event.get("event") == "placed")
