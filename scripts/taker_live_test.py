"""Live fill-quality test for the one taker cell that survived the audit.

    # look first, send nothing
    python scripts/taker_live_test.py --prod

    # send real orders, 1 contract, hard floor
    python scripts/taker_live_test.py --prod --execute --max-trades 40

The audit left exactly one open question. Every net number in it assumes you
take the **displayed touch** at the instant the signal appears. Extreme
imbalance means the side you must cross is the thin one, and the thin side is
the first thing consumed, so the price you actually get may be worse than the
price you measured. No amount of recorded book data can settle that. Real orders
at minimum size can, and cheaply.

This is a measuring instrument, not a strategy. It trades the cell the audit
found and nothing else:

    KXBTCD (BTC hourly strike ladder)
    |OBI| >= 0.9 on the top of book
    entry price 2c to 5c, long-equivalent (a sell at 95-98c is a long of NO at 2-5c)
    spread <= 2c
    inside the last 15 minutes before close, but not the last 60 seconds
    at least 10 contracts showing on the side we cross

Measured on the recorded corpus that cell paid +0.551c to +0.927c per trade.
What this records, per trade, is the displayed touch, the price actually filled,
and the difference. If that difference is near zero the edge is real. If it is
half a cent the edge is gone and the answer is a clean no.

## Why the loss is bounded

One contract entered between 2c and 5c. The worst case is the position going to
zero, which is the entry price: **five cents**. There is no leverage and no
short exposure beyond the complement, because a sell at 97c is economically a
buy of NO at 3c and settles the same way. Forty trades all losing the maximum is
two dollars.

The floor is checked against the exchange's own balance before every entry and
the run stops the moment it is breached. `--execute` is required to send
anything; without it the script prints what it would have done.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.feed_controller import (  # noqa: E402
    FeedController,
    ORDERBOOK_CHANNEL,
)
from kalshi_mm_bot.api.rest import (  # noqa: E402
    CancelOrderRequest,
    CreateOrderRequest,
    KalshiRestClient,
)
from kalshi_mm_bot.api.websocket import KalshiWebSocketClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402

TICKS_PER_CENT = ONE_DOLLAR // 100

# --- the cell, exactly as the audit measured it -------------------------------
SERIES = "KXBTCD"
MIN_OBI = 0.9
ENTRY_MIN = int(0.02 * ONE_DOLLAR)      # long-equivalent price floor
ENTRY_MAX = int(0.05 * ONE_DOLLAR)
MAX_SPREAD_TICKS = 200                  # 2 cents
PHASE_MAX = 900.0                       # last 15 minutes
# Entry + 30s hold + 45s resting exit needs ~80s, so entering later than this
# would still be holding at expiry and the position would settle instead of
# being measured. The audited cell ran 0-900s; this is a conservative subset.
PHASE_MIN = 150.0
MIN_CROSSABLE = 10                      # contracts showing on the side we cross
HOLD_SECONDS = 30.0                     # the horizon the audit measured
EXIT_REST_SECONDS = 40.0   # rest the exit before crossing whatever is left

# --- hard limits --------------------------------------------------------------
SIZE = 1                                # contracts. Not configurable on purpose.
COOLDOWN_SECONDS = 60.0                 # per ticker
MAX_FEED_SILENCE = 20.0


def now() -> float:
    return time.time()


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def fee_cents(price_ticks: float, count: int = 1) -> float:
    p = price_ticks / ONE_DOLLAR
    return 0.07 * count * p * (1.0 - p) * 100.0


def long_equivalent(entry: int, buying: bool) -> int:
    """A sell of YES at 97c is a buy of NO at 3c. Band on the economic price."""

    return entry if buying else ONE_DOLLAR - entry


class Guard:
    """Absolute limits. Deliberately dumb, checked before every order."""

    def __init__(self, floor_cents: int, max_trades: int) -> None:
        self.floor_cents = floor_cents
        self.max_trades = max_trades
        self.trades = 0
        self.halted: str | None = None

    def check_balance(self, balance_cents: int) -> bool:
        if balance_cents <= self.floor_cents:
            self.halted = (
                f"balance ${balance_cents / 100:.2f} at or below floor "
                f"${self.floor_cents / 100:.2f}"
            )
            return False

        return True

    def check_trades(self) -> bool:
        if self.trades >= self.max_trades:
            self.halted = f"reached max trades ({self.max_trades})"
            return False

        return True


async def near_expiry_markets(rest: KalshiRestClient) -> dict[str, float]:
    """KXBTCD tickers closing inside the phase window, with seconds to close."""

    tickers: list[str] = []
    cursor = None

    while True:
        markets, cursor = await rest.list_markets(
            status="open", limit=1000, cursor=cursor, series_ticker=SERIES)

        for market in markets:
            ticker = str(market.get("ticker", ""))

            if ticker.split("-", 1)[0] == SERIES:
                tickers.append(ticker)

        if not cursor:
            break

    if not tickers:
        return {}

    out: dict[str, float] = {}

    for chunk_start in range(0, len(tickers), 100):
        chunk = tickers[chunk_start:chunk_start + 100]
        closes = await rest.get_market_close_times(chunk)

        for ticker, iso in closes.items():
            try:
                close_at = datetime.fromisoformat(
                    iso.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue

            # Absolute close time. Storing "seconds left" would go stale the
            # moment the loop started and silently widen the phase window.
            if PHASE_MIN <= close_at - now() <= PHASE_MAX + 600:
                out[ticker] = close_at

    return out


def evaluate(book, to_close: float, tally: dict | None = None):
    """Return the trade this book implies, or None. Pure, so it is testable.

    `tally` counts why candidates were rejected. Without it a run that fires
    nothing is indistinguishable from a run that is silently broken, and this
    signal is rare enough (about ten an hour across the whole venue) that
    "nothing happened" is the expected output of a working system.
    """

    def no(reason: str):
        if tally is not None:
            tally[reason] = tally.get(reason, 0) + 1

        return None

    if book is None or book.best_bid is None or book.best_ask is None:
        return no("no two-sided book")

    if book.best_ask - book.best_bid > MAX_SPREAD_TICKS:
        return no("spread wider than 2c")

    bid_sz = book.bids[book.best_bid]
    ask_sz = book.asks[book.best_ask]
    total = bid_sz + ask_sz

    if total <= 0:
        return no("empty touch")

    obi = (bid_sz - ask_sz) / total

    if abs(obi) < MIN_OBI:
        return no("imbalance below 0.9")

    buying = obi > 0
    entry = book.best_ask if buying else book.best_bid
    equiv = long_equivalent(entry, buying)

    if not ENTRY_MIN <= equiv <= ENTRY_MAX:
        return no("price outside 2-5c")

    crossable = (ask_sz if buying else bid_sz) / COUNT_SCALE

    if crossable < MIN_CROSSABLE:
        return no("under 10 contracts to cross")

    return {
        "buying": buying,
        "entry": entry,
        "equiv": equiv,
        "obi": obi,
        "bid": book.best_bid,
        "ask": book.best_ask,
        "crossable": crossable,
        "to_close": to_close,
    }


async def send(rest, ticker: str, buying: bool, price: int, count: int, tag: str):
    """Marketable limit order. `side` is the BOOK side being added to, so a buy
    posts on the bid at the ask price and crosses immediately."""

    request = CreateOrderRequest(
        ticker=ticker,
        side="bid" if buying else "ask",
        price=price,
        count=count * COUNT_SCALE,
        client_order_id=f"tlt-{tag}-{int(now() * 1000)}",
        post_only=False,
    )
    return await rest.batch_create_orders([request])


async def realised_fill(rest, ticker: str, since: float) -> dict:
    """What our recent orders on this ticker actually did.

    The field names here were guessed wrong on the first live run and every
    trade reported "no measurable fill" while real orders were executing. These
    are the names the exchange actually returns, confirmed against live orders:

        fill_count_fp             contracts filled, as a decimal string
        yes_price_dollars         the limit we sent, in YES terms
        maker_fill_cost_dollars   what we paid when we RESTED and were hit
        taker_fill_cost_dollars   what we paid when we CROSSED
        maker/taker_fees_dollars  fees, which are zero on the maker side

    Cost is in the currency of the side bought: selling YES at 96c is buying NO
    at 4c, so the YES-equivalent fill price is 1 - cost.
    """

    orders = await rest.get_orders(ticker=ticker, limit=50)
    filled = 0.0
    cost = 0.0
    fees = 0.0
    maker = 0.0
    taker = 0.0
    action = None

    for order in orders:
        client_id = str(order.get("client_order_id", ""))

        if not client_id.startswith("tlt-in-"):
            continue

        try:
            stamp = int(client_id.rsplit("-", 1)[1]) / 1000.0
        except (IndexError, ValueError):
            continue

        if stamp < since - 1.0:
            continue

        count = float(order.get("fill_count_fp") or 0)

        if count <= 0:
            continue

        maker_cost = float(order.get("maker_fill_cost_dollars") or 0)
        taker_cost = float(order.get("taker_fill_cost_dollars") or 0)
        filled += count
        cost += maker_cost + taker_cost
        maker += maker_cost
        taker += taker_cost
        fees += (float(order.get("maker_fees_dollars") or 0)
                 + float(order.get("taker_fees_dollars") or 0))
        action = order.get("action")

    if filled <= 0:
        return {"filled": 0.0}

    per_contract = cost / filled
    fill_yes = per_contract if action == "buy" else 1.0 - per_contract

    return {
        "filled": filled,
        "fill_price_yes": int(round(fill_yes * ONE_DOLLAR)),
        "cost_dollars": cost,
        "fees_dollars": fees,
        "maker_dollars": maker,
        "taker_dollars": taker,
        "was_maker": taker == 0.0,
    }


async def passive_exit(rest, controller, ticker: str, position: int) -> dict:
    """Rest the exit at the touch, cross only what is left. The audited model.

    Crossing straight out pays the spread AND a second fee, which is about 0.67c
    a trade - larger than the edge being measured. The whole project turned
    positive the day it stopped flattening by crossing, so the live test has to
    exit the way the model assumes or it is measuring a worse strategy.

    post_only guarantees the resting order cannot accidentally cross and pay a
    taker fee while trying to avoid one.
    """

    book = controller.orderbooks.get(ticker)

    if book is None or book.best_bid is None or book.best_ask is None:
        return {"exit": "no book"}

    long_position = position > 0
    # A long exits by SELLING at the ask; a short exits by BUYING at the bid.
    price = book.best_ask if long_position else book.best_bid

    try:
        await rest.batch_create_orders([CreateOrderRequest(
            ticker=ticker,
            side="ask" if long_position else "bid",
            price=price,
            count=abs(position),
            client_order_id=f"tlt-rest-{int(now() * 1000)}",
            post_only=True,
        )])
    except Exception as error:  # noqa: BLE001
        log(f"  resting exit rejected ({type(error).__name__}), crossing instead")
        return {"exit": "rest rejected"}

    await asyncio.sleep(EXIT_REST_SECONDS)
    left = (await rest.get_positions((ticker,))).get(ticker, 0)

    if left == 0:
        log(f"  exited passively at {price / 100:.0f}c, no fee")
        return {"exit": "rested", "exit_price": price, "crossed": 0}

    # Cancel whatever is still resting before crossing, or the stub cross and
    # the resting order can both fill and leave us the opposite way round.
    try:
        orders = await rest.get_orders(ticker=ticker, status="resting", limit=50)

        for order in orders:
            if str(order.get("client_order_id", "")).startswith("tlt-rest-"):
                await rest.batch_cancel_orders(
                    [CancelOrderRequest(order_id=str(order["order_id"]))])
    except Exception as error:  # noqa: BLE001
        log(f"  cancel of resting exit failed: {type(error).__name__} {error}")

    return {"exit": "partial", "exit_price": price,
            "crossed": abs(left) // COUNT_SCALE}


async def flatten(rest, controller, ticker: str, log_prefix: str = "  ") -> int:
    """Close any position on this ticker. Called unconditionally after a trade.

    The first live run only ran its exit path when it could *measure* the fill,
    so a measurement bug left five contracts open across three markets. Exiting
    must never depend on the analytics working.
    """

    position = (await rest.get_positions((ticker,))).get(ticker, 0)

    if position == 0:
        return 0

    book = controller.orderbooks.get(ticker)

    if book is None or book.best_bid is None or book.best_ask is None:
        log(f"{log_prefix}no book to exit {ticker}, leaving "
            f"{position / COUNT_SCALE:+.0f} to settle")
        return position

    price = book.best_bid if position > 0 else book.best_ask

    try:
        await send(rest, ticker, position < 0, price,
                   abs(position) // COUNT_SCALE, "flat")
    except Exception as error:  # noqa: BLE001
        log(f"{log_prefix}flatten FAILED {type(error).__name__} {error}")
        return position

    await asyncio.sleep(3.0)
    left = (await rest.get_positions((ticker,))).get(ticker, 0)
    log(f"{log_prefix}flattened {position / COUNT_SCALE:+.0f} -> "
        f"{left / COUNT_SCALE:+.0f}")
    return left


async def run(args) -> None:
    settings = load_settings()
    environment = settings.environment(prod=args.prod)
    auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
    rest = KalshiRestClient(environment.rest_base_url, auth)
    guard = Guard(int(args.floor * 100), args.max_trades)
    journal = Path(args.journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    mode = "LIVE, REAL MONEY" if args.execute else "dry run, sends nothing"

    balance = await rest.get_available_balance_cents()
    log(f"balance ${balance / 100:.2f}, floor ${args.floor:.2f}, mode: {mode}")

    if not guard.check_balance(balance):
        log(f"REFUSING TO START: {guard.halted}")
        await rest.close()
        return

    markets = await near_expiry_markets(rest)

    if not markets:
        log(f"no {SERIES} markets inside the {PHASE_MIN:.0f}-{PHASE_MAX:.0f}s "
            f"window yet. The ladder is tradeable about twelve minutes an hour, "
            f"so this waits for the next one.")

    log(f"watching {len(markets)} {SERIES} markets approaching close "
        f"(trading only inside the final {PHASE_MAX / 60:.0f} minutes)")
    ws = KalshiWebSocketClient(environment.ws_url, auth)
    controller = FeedController(rest=rest, ws=ws)
    await controller.connect()

    initial = tuple(markets)

    last_trigger: dict[str, float] = {}
    tally: dict[str, int] = {}
    stats = {"updates": 0, "last_event": now(), "stop": False}
    pending: asyncio.Queue = asyncio.Queue(maxsize=1)
    signals = 0

    subscribe_queue: asyncio.Queue = asyncio.Queue()

    async def pump() -> None:
        """Keep draining the feed forever, and own every socket operation.

        Subscribing reads the socket to await its confirmation, so a second
        coroutine calling subscribe() while this one sits in recv() raises
        ConcurrencyError and the subscription is silently lost. The first
        version did exactly that: the refresh task failed every time and the
        run watched zero markets for half an hour without trading.

        This has to run concurrently with trading. The first version awaited the
        30-second hold inside the same loop that pumped the websocket, so the
        book used to price the exit was thirty seconds stale - and a stale book
        is exactly how you send an order at a price that no longer exists.
        """

        while not stats["stop"]:
            # Drain pending subscriptions here, where nothing else is reading.
            while not subscribe_queue.empty():
                batch = await subscribe_queue.get()

                try:
                    await controller.subscribe(
                        batch, channels=(ORDERBOOK_CHANNEL,))
                    stats["last_event"] = now()
                    log(f"  subscribed {len(batch)} market(s), watching "
                        f"{len(markets)}")
                except Exception as error:  # noqa: BLE001
                    log(f"  subscribe FAILED: {type(error).__name__} {error}")

            try:
                ticker = await asyncio.wait_for(controller.recv(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except EOFError:
                stats["stop"] = True
                return

            stats["last_event"] = now()
            stats["updates"] += 1

            if ticker is None or ticker not in markets:
                continue

            to_close = markets[ticker] - now()

            if not PHASE_MIN <= to_close <= PHASE_MAX:
                tally["outside the final 15 minutes"] = tally.get(
                    "outside the final 15 minutes", 0) + 1
                continue

            if now() - last_trigger.get(ticker, 0.0) < COOLDOWN_SECONDS:
                continue

            trade = evaluate(controller.orderbooks.get(ticker), to_close, tally)

            if trade is None:
                continue

            last_trigger[ticker] = now()

            # One trade at a time. A full queue means we are mid-trade, and
            # dropping the signal is correct: it will come round again.
            try:
                pending.put_nowait((ticker, trade))
            except asyncio.QueueFull:
                pass

    async def refresh() -> None:
        """Re-discover markets as hourly strikes expire and new ones open.

        The ladder is only tradeable for about twelve minutes an hour (the
        150-900s window before each close), so a run long enough to collect a
        sample spans several expiries. Discovering once at startup would watch
        a set of markets that are all closed within the hour.
        """

        while not stats["stop"]:
            await asyncio.sleep(120.0)

            try:
                fresh = await near_expiry_markets(rest)
            except Exception as error:  # noqa: BLE001
                log(f"  market refresh failed: {type(error).__name__} {error}")
                continue

            added = [t for t in fresh if t not in markets]
            markets.clear()
            markets.update(fresh)

            if added:
                await subscribe_queue.put(tuple(added))

    if initial:
        await subscribe_queue.put(initial)

    pump_task = asyncio.create_task(pump())
    refresh_task = asyncio.create_task(refresh())
    deadline = now() + args.duration_sec

    while now() < deadline and guard.halted is None and not stats["stop"]:
        # Silence only means danger when we are subscribed to something. With
        # no markets in the window there is legitimately nothing to receive,
        # and the ladder is untradeable for about 48 minutes an hour.
        if markets and now() - stats["last_event"] > MAX_FEED_SILENCE:
            guard.halted = f"feed silent for {now() - stats['last_event']:.0f}s"
            break

        try:
            ticker, trade = await asyncio.wait_for(pending.get(), timeout=5.0)
        except asyncio.TimeoutError:
            continue

        signals += 1
        side = "BUY " if trade["buying"] else "SELL"
        log(f"SIGNAL {signals}: {side} {ticker} @ {trade['entry'] / 100:.0f}c "
            f"(equiv {trade['equiv'] / 100:.0f}c) obi {trade['obi']:+.2f} "
            f"size showing {trade['crossable']:.0f} t-{trade['to_close']:.0f}s")

        record = {
            "ts": now(), "ticker": ticker, **trade,
            "displayed_touch": trade["entry"], "executed": False,
        }

        if not args.execute:
            with journal.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            continue

        balance = await rest.get_available_balance_cents()

        if not guard.check_balance(balance) or not guard.check_trades():
            break

        sent_at = now()

        try:
            await send(rest, ticker, trade["buying"], trade["entry"], SIZE, "in")
        except Exception as error:  # noqa: BLE001
            log(f"  entry REJECTED: {type(error).__name__} {error}")
            record["error"] = str(error)
            with journal.open("a") as handle:
                handle.write(json.dumps(record) + "\n")
            continue

        guard.trades += 1
        await asyncio.sleep(2.5)
        fill = await realised_fill(rest, ticker, sent_at)
        record.update({k: v for k, v in fill.items()})

        if fill["filled"] > 0:
            # The number this whole test exists to produce: did we get the
            # price that was showing when the signal fired?
            slip = (fill["fill_price_yes"] - trade["entry"]) / TICKS_PER_CENT
            slip = slip if trade["buying"] else -slip
            record["slippage_cents"] = slip
            how = "MAKER, no fee" if fill["was_maker"] else "taker"
            log(f"  filled {fill['filled']:.0f} at "
                f"{fill['fill_price_yes'] / 100:.0f}c vs displayed "
                f"{trade['entry'] / 100:.0f}c -> slippage {slip:+.2f}c "
                f"({how}, fees ${fill['fees_dollars']:.4f})")
            record["executed"] = True
        else:
            log("  order sent but nothing filled yet")

        # Hold the measured horizon, then leave the way the audit assumed:
        # rest at the touch, cross only the stub. Both steps run whatever the
        # measurement did, because a stranded position is the one outcome that
        # actually costs money.
        await asyncio.sleep(HOLD_SECONDS)
        held = (await rest.get_positions((ticker,))).get(ticker, 0)

        if held:
            record.update(await passive_exit(rest, controller, ticker, held))

        left = await flatten(rest, controller, ticker)
        record["left_open"] = left

        balance_after = await rest.get_available_balance_cents()
        record["balance_after"] = balance_after
        record["pnl_cents"] = balance_after - balance
        log(f"  balance ${balance_after / 100:.2f} ({balance_after - balance:+d}c)")

        with journal.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    stats["stop"] = True
    pump_task.cancel()
    refresh_task.cancel()
    updates = stats["updates"]

    if guard.halted:
        log(f"HALTED: {guard.halted}")

    final = await rest.get_available_balance_cents()
    log(f"done. {updates} book updates, signals {signals}, trades {guard.trades}, "
        f"balance ${final / 100:.2f}. journal -> {journal}")

    if tally:
        log("why candidates were rejected (a working run rejects nearly all of them):")

        for reason, count in sorted(tally.items(), key=lambda kv: -kv[1]):
            log(f"    {count:>7}  {reason}")
    await rest.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod", action="store_true")
    parser.add_argument("--execute", action="store_true",
                        help="send real orders. Without this nothing is sent.")
    parser.add_argument("--floor", type=float, default=25.0,
                        help="stop if the account balance reaches this, in dollars")
    parser.add_argument("--max-trades", type=int, default=40)
    parser.add_argument("--duration-sec", type=float, default=3600.0)
    parser.add_argument("--journal", default="/var/tmp/taker_live_test.jsonl")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
