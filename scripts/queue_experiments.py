"""Learn what actually fills, by placing small real orders under varied conditions.

    python scripts/queue_experiments.py --trials 12          # dry run
    python scripts/queue_experiments.py --trials 12 --execute

The simulator and the live account disagree violently. The queue-aware fill
model gave one strategy a 31% fill rate over 20 minutes; seven real resting
orders filled 0%. The gap is queue position, and the first live attempt ignored
the one number that determines it: there were **35,249 contracts ahead of us**
at the touch of the market we were resting in.

That is a parameter, not a law. This harness varies the parameters we control
and records what happens, so the fill model can be calibrated against reality
instead of assumed.

What we control:

* **Where we rest.** At the touch (back of the queue), inside the spread (front
  of a new level, needs >=2 ticks of spread), or crossing (immediate, but taker).
* **Which queue we join.** Depth at our price is visible before we order. A
  thin queue in a market with flow is the whole game.
* **How long we wait.** Short rests measure whether flow reaches us; long rests
  measure whether the price walks through us.

What we do not control: whether anyone trades, and who else is quoting.

Each trial is one contract. Positions are flattened at the end of each trial, and
every fill records the fee Kalshi actually charged - which also answers the
maker-fee question the whole model forks on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.parser import FEE_FIELDS, parse_fill_fee_micros  # noqa: E402
from kalshi_mm_bot.api.rest import (  # noqa: E402
    CancelOrderRequest,
    CreateOrderRequest,
    KalshiRestClient,
)
from kalshi_mm_bot.config import load_settings  # noqa: E402
from kalshi_mm_bot.market.price import (  # noqa: E402
    COUNT_SCALE,
    MONEY_SCALE,
    ONE_DOLLAR,

    parse_money_fp,
    parse_price_fp,
)

CONFIRMATION = "RUN QUEUE EXPERIMENTS WITH REAL MONEY"
PREFIX = "qexp"
MODES = ("touch", "inside", "cross")


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass
class Trial:
    """One order placed under known conditions, and what became of it.

    Every field here is either a condition we chose or an outcome we observed.
    The point of the record is that a later reader can tell which was which
    without having been present, so anything ambiguous is spelled out rather
    than inferred.
    """

    ticker: str
    mode: str
    rest_seconds: float
    spread_ticks: int
    depth_ahead: float
    our_price: int
    mid: int
    filled: bool
    # True execution latency, from the exchange's own timestamp. None when the
    # payload carries no usable time.
    seconds_to_fill: float | None
    is_taker: bool | None
    fee_micros: int | None
    # Wall-clock provenance. Trials from different sessions and different market
    # regimes end up in the same directory, and "which run was this" is not
    # recoverable from the file name once two runs overlap.
    recorded_at: str = ""
    # The regime variable. A 15-minute window at 12 minutes out and the same
    # window at 30 seconds out are different markets: the price has migrated to
    # the tail, the spread has collapsed to a tenth of a cent and the depth has
    # fallen 11x. Pooling trials across that without recording where in the
    # window each one sat produces an average of two unlike things.
    seconds_to_close: float | None = None
    # Book at the moment we filled, for markout. Without it there is no way to
    # tell a good fill from one that was immediately run over.
    mid_at_fill: int | None = None
    # How long after the execution the book above was sampled. This is the
    # markout horizon, and it is not controlled - report it, never assume it.
    mid_lag_seconds: float | None = None
    # How long our polling took to notice. A property of the harness, kept so a
    # later reader can tell it apart from seconds_to_fill.
    seconds_to_detect: float | None = None
    note: str = ""


def _seconds_to_close(market: dict) -> float | None:
    """Seconds of market left, or None if it cannot be determined.

    None rather than a negative number for an already-closed market: a screen
    that ranks on time remaining must not read "closed an hour ago" as urgent.
    """

    stamp = market.get("close_time") or market.get("expiration_time")

    if not isinstance(stamp, str) or not stamp:
        return None

    try:
        close = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None

    remaining = (close - datetime.now(UTC)).total_seconds()
    return remaining if remaining > 0 else None


def _fill_time(fill: dict) -> float | None:
    """Exchange-stamped execution time as a unix timestamp."""

    stamp = fill.get("created_time")

    if isinstance(stamp, str) and stamp:
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass

    try:
        return float(fill["ts"])
    except (KeyError, TypeError, ValueError):
        return None


def _fill_latency_seconds(fill: dict, placed_at_wall: float) -> float | None:
    """Seconds from placing the order to the exchange executing it."""

    executed = _fill_time(fill)
    return None if executed is None else max(0.0, executed - placed_at_wall)


def _fill_age_seconds(fill: dict) -> float | None:
    """How long ago the execution happened, as of now."""

    executed = _fill_time(fill)
    return None if executed is None else max(0.0, time.time() - executed)


async def book_state(rest: KalshiRestClient, ticker: str) -> dict | None:
    """Bid, ask and - the number that matters - size resting at each touch."""

    try:
        raw = await rest._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": 10}
        )
    except Exception:
        return None

    book = raw.get("orderbook_fp") or {}
    yes = book.get("yes_dollars") or []
    no = book.get("no_dollars") or []

    if not yes or not no:
        return None

    bids = {parse_price_fp(p): _num(s) for p, s in yes}
    # NO prices are the YES ask side mirrored about a dollar.
    asks = {ONE_DOLLAR - parse_price_fp(p): _num(s) for p, s in no}

    best_bid = max(bids)
    best_ask = min(asks)

    if not best_bid < best_ask:
        return None

    return {
        "bid": best_bid,
        "ask": best_ask,
        "bid_depth": bids[best_bid],
        "ask_depth": asks[best_ask],
        "spread": best_ask - best_bid,
        "mid": (best_bid + best_ask) // 2,
    }


async def candidates(rest: KalshiRestClient, args: argparse.Namespace) -> list[dict]:
    """Quotable markets, annotated with the depth we would be queueing behind.

    `--series` matters more than it looks. Ranking the whole exchange by volume
    picks whatever is busiest right now, which in the afternoon is baseball; a
    run aimed at understanding 15-minute crypto windows then spends its budget
    measuring queues in markets that behave nothing like them. Volume is not the
    variable under study.

    A series filter also skips the paged /events sweep entirely, which is worth
    doing on its own: rolling short-dated series keep exactly one market open,
    and it is routinely missing from a volume-ranked sweep because its 24h
    volume field reads zero until the window has been alive for a day - which
    for a 15-minute market is never.
    """

    if args.series:
        return await _series_candidates(rest, args)

    found: list[dict] = []
    cursor = None

    for _ in range(6):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}

        if cursor:
            params["cursor"] = cursor

        data = await rest._request("GET", "/events", params=params)
        cursor = data.get("cursor")

        for event in data.get("events", []):
            for market in event.get("markets", []) or []:
                if market.get("mve_collection_ticker"):
                    continue

                bid = _num(market.get("yes_bid_dollars"))
                ask = _num(market.get("yes_ask_dollars"))
                volume = _num(market.get("volume_24h_fp"))

                if 0.10 < bid < ask < 0.90 and volume >= args.min_volume:
                    found.append(
                        {
                            "ticker": market["ticker"],
                            "volume": volume,
                            "close_time": market.get("close_time"),
                        }
                    )

        if not cursor:
            break

    found.sort(key=lambda m: -m["volume"])
    annotated = []

    for market in found[: args.probe]:
        state = await book_state(rest, market["ticker"])

        if state is None:
            continue

        annotated.append({**market, **state})

    return annotated


async def _series_candidates(
    rest: KalshiRestClient, args: argparse.Namespace
) -> list[dict]:
    """Open markets of the named series, annotated with queue depth.

    Deliberately does not apply the volume floor or the 0.10-0.90 price band
    used for the exchange-wide sweep: the caller named this series on purpose,
    and a 15-minute window spends its final minutes in exactly the tail those
    filters exclude.
    """

    annotated: list[dict] = []

    for series in args.series:
        data = await rest._request(
            "GET",
            "/markets",
            params={"status": "open", "limit": 200, "series_ticker": series},
        )

        for market in data.get("markets", []) or []:
            bid = _num(market.get("yes_bid_dollars"))
            ask = _num(market.get("yes_ask_dollars"))

            if not 0 < bid < ask < 1:
                continue

            state = await book_state(rest, market["ticker"])

            if state is None:
                continue

            annotated.append(
                {
                    "ticker": market["ticker"],
                    # volume_fp is the market's own lifetime volume; volume_24h_fp
                    # is zero for anything younger than a day.
                    "volume": _num(market.get("volume_fp")),
                    "close_time": market.get("close_time"),
                    **state,
                }
            )

    annotated.sort(key=lambda m: -m["volume"])
    return annotated


async def flatten(rest: KalshiRestClient, ticker: str) -> None:
    """Leave no position behind. Crosses if it must - correctness over price."""

    positions = await rest.get_positions((ticker,))
    position = positions.get(ticker, 0)

    if position == 0:
        return

    state = await book_state(rest, ticker)

    if state is None:
        log(f"  cannot flatten {ticker}: no book")
        return

    side = "ask" if position > 0 else "bid"
    price = state["bid"] if position > 0 else state["ask"]

    await rest.batch_create_orders(
        [
            CreateOrderRequest(
                ticker=ticker,
                side=side,
                price=price,
                count=abs(position),
                client_order_id=f"{PREFIX}-flat-{int(time.time())}",
                post_only=False,
            )
        ]
    )
    await asyncio.sleep(2.0)
    remaining = (await rest.get_positions((ticker,))).get(ticker, 0)
    log(f"  flattened {ticker}: {position / COUNT_SCALE:+.0f} -> {remaining / COUNT_SCALE:+.0f}")


async def run_trial(
    rest: KalshiRestClient,
    market: dict,
    mode: str,
    rest_seconds: float,
    seen_trades: set[str],
) -> Trial | None:
    ticker = market["ticker"]
    state = await book_state(rest, ticker)

    if state is None:
        return None

    spread = state["spread"]

    if mode == "inside" and spread <= 100:
        return None  # no room to improve without crossing

    if mode == "touch":
        price, post_only = state["bid"], True
    elif mode == "inside":
        price, post_only = state["bid"] + 100, True
    else:
        price, post_only = state["ask"], False  # cross the spread

    client_id = f"{PREFIX}-{int(time.time() * 1000)}"
    started = time.monotonic()
    placed_at_wall = time.time()

    response = await rest.batch_create_orders(
        [
            CreateOrderRequest(
                ticker=ticker,
                side="bid",
                price=price,
                count=1 * COUNT_SCALE,
                client_order_id=client_id,
                post_only=post_only,
            )
        ]
    )
    orders = response.get("orders") or []
    order_id = str(orders[0].get("order_id", "")) if orders else ""

    trial = Trial(
        ticker=ticker,
        mode=mode,
        rest_seconds=rest_seconds,
        spread_ticks=spread,
        # Depth at the price we actually joined. Recording 0.0 for the non-touch
        # modes used to make "we jumped the queue" and "we never looked"
        # indistinguishable in the data; an empty new level really is zero ahead
        # of us, and a crossing order really does have none, but those are
        # findings rather than placeholders.
        depth_ahead=(
            state["bid_depth"]
            if mode == "touch"
            else 0.0  # inside: new price level, nobody there. cross: immediate.
        ),
        recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        seconds_to_close=_seconds_to_close(market),
        our_price=price,
        mid=state["mid"],
        filled=False,
        seconds_to_fill=None,
        is_taker=None,
        fee_micros=None,
    )

    if not order_id:
        trial.note = f"rejected: {orders[0] if orders else response}"
        return trial

    deadline = time.monotonic() + rest_seconds

    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)
        data = await rest._request("GET", "/portfolio/fills", params={"limit": 50})

        for fill in data.get("fills") or []:
            if str(fill.get("order_id")) != order_id:
                continue

            trade_id = str(fill.get("trade_id"))

            if trade_id in seen_trades:
                continue

            seen_trades.add(trade_id)
            trial.filled = True
            # Detection time is a property of THIS LOOP, not of the market. It
            # can only ever be a multiple of the poll interval, which is why an
            # earlier version reported that every fill landed in "2 seconds" -
            # that was the sleep above, reported back as a measurement. The
            # exchange stamps the real execution time, so use it.
            trial.seconds_to_fill = _fill_latency_seconds(fill, placed_at_wall)
            trial.seconds_to_detect = time.monotonic() - started
            trial.is_taker = bool(fill.get("is_taker"))
            # None means the payload did not report a fee. Never coerce that to
            # zero: it is the difference between "free" and "we cannot read it".
            trial.fee_micros = parse_fill_fee_micros(fill)
            # The book AFTER the fill, not at it. We learn of the fill up to one
            # poll interval late and then spend a round trip fetching the book,
            # so this is a markout at an uncontrolled horizon of roughly 0-4
            # seconds. Record how stale it is so the horizon is a known quantity
            # rather than a hidden one; a markout whose horizon is unknown is
            # not comparable to anything.
            at_fill = await book_state(rest, ticker)
            trial.mid_at_fill = at_fill["mid"] if at_fill else None
            trial.mid_lag_seconds = _fill_age_seconds(fill)
            break

        if trial.filled:
            break

    if not trial.filled:
        await rest.batch_cancel_orders([CancelOrderRequest(order_id=order_id)])

    await flatten(rest, ticker)
    return trial


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    environment = settings.environment(prod=True)
    rest = KalshiRestClient(
        environment.rest_base_url,
        KalshiAuth(settings.api_key_id, settings.private_key_path),
    )
    trials: list[Trial] = []
    touched: set[str] = set()

    try:
        balance = await rest.get_available_balance_cents() / 100
        log(f"balance ${balance:,.2f}")

        if balance > args.max_balance:
            raise SystemExit("balance above --max-balance; this is a probe, not a book")

        markets = await candidates(rest, args)
        log(f"{len(markets)} quotable markets probed for depth")

        thin = [m for m in markets if m["bid_depth"] <= args.thin_depth]
        deep = [m for m in markets if m["bid_depth"] > args.thin_depth]
        wide = [m for m in markets if m["spread"] > 100]
        log(
            f"  thin queue (<={args.thin_depth:g} ahead): {len(thin)}   "
            f"deep: {len(deep)}   spread>1c: {len(wide)}"
        )

        for market in sorted(markets, key=lambda m: m["bid_depth"])[:5]:
            log(
                f"    {market['ticker'][:38]:<40} spread {market['spread'] / 100:.0f}c "
                f"depth ahead {market['bid_depth']:,.0f} vol {market['volume']:,.0f}"
            )

        plan = []

        for mode in args.modes:
            pool = wide if mode == "inside" else (thin or markets)

            for market in pool[: args.per_mode]:
                for rest_seconds in args.rests:
                    plan.append((market, mode, float(rest_seconds)))

        # Built mode-major, so a plain slice would spend the whole budget on
        # the first mode and leave the others unmeasured. Round-robin instead.
        by_mode: dict[str, list] = {}

        for entry in plan:
            by_mode.setdefault(entry[1], []).append(entry)

        interleaved = []

        while any(by_mode.values()):
            for mode in list(by_mode):
                if by_mode[mode]:
                    interleaved.append(by_mode[mode].pop(0))

        plan = interleaved[: args.trials]
        log(f"planned {len(plan)} trial(s), 1 contract each")

        if not args.execute:
            log("DRY RUN - nothing sent.")
            return

        typed = input(f'Type "{CONFIRMATION}" to proceed: ').strip()

        if typed != CONFIRMATION:
            raise SystemExit("not confirmed")

        for index, (market, mode, rest_seconds) in enumerate(plan, start=1):
            live = await rest.get_available_balance_cents() / 100

            if balance - live > args.max_loss:
                log(f"loss limit reached (${balance - live:.2f}); stopping")
                break

            log(f"trial {index}/{len(plan)}: {mode} on {market['ticker'][:34]} for {rest_seconds:g}s")
            touched.add(market["ticker"])
            trial = await run_trial(rest, market, mode, rest_seconds, set())

            if trial is None:
                log("  skipped (book unusable)")
                continue

            trials.append(trial)
            log(
                f"  {'FILLED' if trial.filled else 'no fill'}"
                + (
                    f" in {trial.seconds_to_fill:.0f}s, taker={trial.is_taker}, "
                    f"fee ${(trial.fee_micros or 0) / MONEY_SCALE:.4f}"
                    if trial.filled
                    else f" (depth ahead {trial.depth_ahead:,.0f})"
                )
            )
    finally:
        for ticker in touched:
            try:
                for order in await rest.get_orders(ticker=ticker, status="resting"):
                    if str(order.get("client_order_id", "")).startswith(PREFIX):
                        await rest.batch_cancel_orders(
                            [CancelOrderRequest(order_id=str(order["order_id"]))]
                        )
                await flatten(rest, ticker)
            except Exception as error:
                log(f"cleanup {ticker}: {type(error).__name__} {error}")

        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([asdict(t) for t in trials], indent=2))
        log(f"wrote {out}")
        report(trials)

        try:
            log(f"final balance ${await rest.get_available_balance_cents() / 100:,.2f}")
        finally:
            await rest.close()


def report(trials: list[Trial]) -> None:
    if not trials:
        print("\nno trials completed")
        return

    print(f"\n{'mode':<8}{'trials':>7}{'filled':>8}{'rate':>8}{'med depth':>11}{'med fill s':>12}")

    for mode in MODES:
        subset = [t for t in trials if t.mode == mode]

        if not subset:
            continue

        filled = [t for t in subset if t.filled]
        times = [t.seconds_to_fill for t in filled if t.seconds_to_fill is not None]
        depths = [t.depth_ahead for t in subset]
        print(
            f"{mode:<8}{len(subset):>7}{len(filled):>8}{len(filled) / len(subset):>8.0%}"
            f"{statistics.median(depths):>11,.0f}"
            f"{(statistics.median(times) if times else float('nan')):>12.0f}"
        )

    filled_trials = [t for t in trials if t.filled]
    fills = [t for t in filled_trials if t.fee_micros is not None]
    unreadable = [t for t in filled_trials if t.fee_micros is None]

    if unreadable:
        # Loud, because this is exactly how the previous version lied: it read a
        # field name Kalshi had renamed, defaulted the miss to "0", and reported
        # every fill on the ledger as free - takers included, who are charged
        # about 1.3c. Never let a read failure sum into a total.
        print(
            f"\n!! {len(unreadable)} of {len(filled_trials)} fill(s) reported NO fee field."
        )
        print("   These are UNREADABLE, not free. Check the payload before trusting")
        print(f"   any fee conclusion. Known field names: {', '.join(FEE_FIELDS)}")

    if fills:
        print(f"\n{'price':>8}{'taker':>7}{'fee charged':>14}")
        for trial in fills:
            print(
                f"{trial.our_price / ONE_DOLLAR:>8.2f}{str(trial.is_taker):>7}"
                f"{trial.fee_micros / MONEY_SCALE:>14.4f}"
            )

        makers = [t for t in fills if not t.is_taker]
        takers = [t for t in fills if t.is_taker]

        if makers:
            charged = sum(t.fee_micros for t in makers)
            verdict = "maker fees are REAL" if charged else "NO maker fee charged"
            print(
                f"\nMAKER FILLS: {len(makers)} readable, total fee "
                f"${charged / MONEY_SCALE:.4f} -> {verdict}"
            )

            # A zero maker total only means something if the same reader saw a
            # non-zero taker total. Without that control, "no fee" and "no
            # working fee reader" look identical.
            if not charged:
                taker_charged = sum(t.fee_micros for t in takers)

                if takers and taker_charged:
                    print(
                        f"   control: {len(takers)} taker fill(s) charged "
                        f"${taker_charged / MONEY_SCALE:.4f}, so the reader works."
                    )
                else:
                    print(
                        "   NO CONTROL: no taker fill charged anything either, so a "
                        "broken reader is indistinguishable from a free market. "
                        "Cross once before believing this."
                    )
        else:
            print("\nno maker fills yet - the fee question is still open")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--trials", type=int, default=12)
    parser.add_argument("--per-mode", type=int, default=2)
    parser.add_argument("--rests", type=float, nargs="+", default=[30, 180])
    parser.add_argument("--probe", type=int, default=40, help="Markets to read depth for.")
    parser.add_argument(
        "--series",
        action="append",
        help="Restrict to these series (e.g. KXBTC15M). Repeatable. Skips the "
        "exchange-wide volume ranking, which otherwise picks whatever is busiest "
        "rather than what is under study.",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=MODES,
        default=list(MODES),
        help="Which quoting modes to test. Use --modes touch to gather maker "
        "fills only, which is what a markout distribution needs.",
    )
    parser.add_argument("--thin-depth", type=float, default=100.0)
    parser.add_argument("--min-volume", type=float, default=2000.0)
    parser.add_argument("--max-loss", type=float, default=5.0)
    parser.add_argument("--max-balance", type=float, default=200.0)
    parser.add_argument("--output", default="data/queue_experiments.json")
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
