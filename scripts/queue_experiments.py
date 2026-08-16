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
    ticker: str
    mode: str
    rest_seconds: float
    spread_ticks: int
    depth_ahead: float
    our_price: int
    mid: int
    filled: bool
    seconds_to_fill: float | None
    is_taker: bool | None
    fee_micros: int | None
    note: str = ""


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
    """Quotable markets, annotated with the depth we would be queueing behind."""

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
                    found.append({"ticker": market["ticker"], "volume": volume})

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
        depth_ahead=state["bid_depth"] if mode == "touch" else 0.0,
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
            trial.seconds_to_fill = time.monotonic() - started
            trial.is_taker = bool(fill.get("is_taker"))
            trial.fee_micros = parse_money_fp(str(fill.get("fees_paid_dollars", "0")))
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

        for mode in MODES:
            pool = wide if mode == "inside" else (thin or markets)

            for market in pool[: args.per_mode]:
                for rest_seconds in args.rests:
                    plan.append((market, mode, float(rest_seconds)))

        plan = plan[: args.trials]
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

    fills = [t for t in trials if t.filled and t.fee_micros is not None]

    if fills:
        print(f"\n{'price':>8}{'taker':>7}{'fee charged':>14}")
        for trial in fills:
            print(
                f"{trial.our_price / ONE_DOLLAR:>8.2f}{str(trial.is_taker):>7}"
                f"{trial.fee_micros / MONEY_SCALE:>14.4f}"
            )

        makers = [t for t in fills if not t.is_taker]

        if makers:
            charged = sum(t.fee_micros or 0 for t in makers)
            print(
                f"\nMAKER FILLS: {len(makers)}, total fee ${charged / MONEY_SCALE:.4f} -> "
                + ("maker fees are REAL" if charged else "NO maker fee charged")
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
