"""Buy the one fact the whole model rests on: what Kalshi actually charges us.

    python scripts/calibrate_fees.py                 # dry run, sends nothing
    python scripts/calibrate_fees.py --execute       # asks for typed confirmation

Every profit figure in this project forks on one unknown: whether a resting
(maker) fill is charged the 0.07 taker formula, a reduced rate, or nothing.
Published summaries disagree. The difference is roughly 5x on the addressable
opportunity, and no amount of backtesting resolves it, because the answer lives
on Kalshi's ledger rather than in the order book.

A handful of one-contract round trips settles it for a couple of dollars.

This is the only script in the repo that can spend money, so it is built to be
boring and hard to misuse:

* **Dry run by default.** `--execute` plus a typed confirmation phrase.
* **One contract per order**, a hard cap on round trips, and a loss limit that
  aborts the run.
* **Entry is `post_only`.** The order rests or is rejected - it cannot cross.
  That is what makes the fill a *maker* fill, which is the measurement we came
  for; a crossing order would answer the wrong question.
* **Refuses to run on a funded account.** Above `--max-balance` it stops, so it
  can never be aimed at a real book by accident.
* **Cancels everything and reports the position on exit**, including on crash.

It answers one question and then gets out. It is not a trading strategy and
must not be turned into one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
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
from kalshi_mm_bot.market.fees import KalshiFeeModel, calibrate_from_fills  # noqa: E402
from kalshi_mm_bot.market.price import (  # noqa: E402
    COUNT_SCALE,
    MONEY_SCALE,
    ONE_DOLLAR,
    parse_count_fp,
    parse_money_fp,
    parse_price_fp,
)

CONFIRMATION = "CALIBRATE FEES WITH REAL MONEY"
PREFIX = "feecal"


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def pick_market(rest: KalshiRestClient, args: argparse.Namespace) -> dict | None:
    """A liquid market priced near 50c, where the fee signal is largest.

    The fee is proportional to P*(1-P), so a midpoint market maximises the
    number we are trying to read and makes a reduced schedule easy to tell from
    the full one. Cheap tail markets would round everything to a cent and hide
    the rate.
    """

    # Via /events, not /markets: the latter is flooded with auto-generated
    # parlay combos - 248,501 of the first 251,000 records in a full scan - so
    # paging it finds nothing tradable before the cursor runs out.
    best = None
    cursor = None

    for _ in range(8):
        params = {"status": "open", "limit": 200, "with_nested_markets": "true"}

        if cursor:
            params["cursor"] = cursor

        data = await rest._request("GET", "/events", params=params)
        cursor = data.get("cursor")
        nested = [
            market
            for event in data.get("events", [])
            for market in (event.get("markets") or [])
        ]

        for market in nested:
            if market.get("mve_collection_ticker"):
                continue

            bid = _num(market.get("yes_bid_dollars"))
            ask = _num(market.get("yes_ask_dollars"))
            volume = _num(market.get("volume_24h_fp"))

            if not (0.30 <= bid < ask <= 0.70):
                continue

            if volume < args.min_volume or (ask - bid) > args.max_spread / 100:
                continue

            score = volume / max(0.01, ask - bid)

            if best is None or score > best[0]:
                best = (score, market)

        if not cursor:
            break

    return best[1] if best else None


async def collect_fills(rest: KalshiRestClient) -> list[dict]:
    data = await rest._request("GET", "/portfolio/fills", params={"limit": 200})
    return data.get("fills") or []


async def wait_for_fill(
    rest: KalshiRestClient,
    ticker: str,
    order_id: str,
    timeout: float,
) -> bool:
    """Wait for a real execution against `order_id`.

    Confirmed against the fills ledger, not by the order disappearing from the
    resting list. The first version of this inferred a fill from absence and
    reported two fills in the same second the orders were placed - the orders
    had simply not appeared as resting yet. Nothing had traded. An execution is
    only an execution when the exchange says a trade happened; anything else is
    a guess, and guessing you are filled is how a bot ends up with a position it
    does not know about.
    """

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        await asyncio.sleep(2.0)

        for fill in await collect_fills(rest):
            if str(fill.get("order_id")) == order_id:
                return True

    return False


async def _run(args: argparse.Namespace) -> None:
    settings = load_settings()
    environment = settings.environment(prod=True)
    rest = KalshiRestClient(
        environment.rest_base_url,
        KalshiAuth(settings.api_key_id, settings.private_key_path),
    )

    try:
        balance = await rest.get_available_balance_cents() / 100
        log(f"balance ${balance:,.2f}")

        if balance > args.max_balance:
            raise SystemExit(
                f"balance ${balance:,.2f} exceeds --max-balance ${args.max_balance:,.2f}. "
                "This probe is for a small dedicated account, not a funded book."
            )

        if args.ticker:
            # An explicit target, for when the caller has already measured which
            # book is moving. 24h volume is a poor proxy for current flow, and a
            # resting order in a quiet market simply never fills.
            fetched = await rest._request("GET", "/markets", params={"tickers": args.ticker})
            market = (fetched.get("markets") or [None])[0]
        else:
            market = await pick_market(rest, args)

        if market is None:
            raise SystemExit("no suitable midpoint market found right now; try again later")

        ticker = market["ticker"]
        bid = parse_price_fp(market["yes_bid_dollars"])
        ask = parse_price_fp(market["yes_ask_dollars"])
        log(
            f"target {ticker} bid ${bid / ONE_DOLLAR:.2f} ask ${ask / ONE_DOLLAR:.2f} "
            f"vol24h {_num(market.get('volume_24h_fp')):,.0f}"
        )

        plan_cost = args.round_trips * (ask - bid) / ONE_DOLLAR + args.round_trips * 0.04
        log(
            f"plan: up to {args.round_trips} round trip(s) of 1 contract; "
            f"worst realistic cost about ${plan_cost:.2f} in spread and fees"
        )

        if not args.execute:
            log("DRY RUN - nothing sent. Re-run with --execute to place real orders.")
            return

        typed = input(f'Type "{CONFIRMATION}" to proceed: ').strip()

        if typed != CONFIRMATION:
            raise SystemExit("not confirmed; nothing sent")

        before = {str(f.get("trade_id")) for f in await collect_fills(rest)}
        start_balance = balance
        placed = 0

        for trip in range(1, args.round_trips + 1):
            live_balance = await rest.get_available_balance_cents() / 100

            if start_balance - live_balance > args.max_loss:
                log(f"loss limit hit (${start_balance - live_balance:.2f}); stopping")
                break

            fresh = (await rest._request("GET", "/markets", params={"tickers": ticker}))[
                "markets"
            ][0]
            live_bid = parse_price_fp(fresh["yes_bid_dollars"])
            live_ask = parse_price_fp(fresh["yes_ask_dollars"])
            # Step inside when the spread allows it - resting at the touch means
            # queueing behind everyone already there, and this probe needs a
            # fill more than it needs a good price.
            entry = live_bid + 100 if (live_ask - live_bid) > 100 else live_bid

            client_id = f"{PREFIX}-{int(time.time())}-{trip}"
            log(f"trip {trip}: resting BUY 1 @ ${entry / ONE_DOLLAR:.2f} (post_only)")

            response = await rest.batch_create_orders(
                [
                    CreateOrderRequest(
                        ticker=ticker,
                        side="bid",
                        price=entry,
                        count=1 * COUNT_SCALE,
                        client_order_id=client_id,
                        post_only=True,
                    )
                ]
            )
            orders = response.get("orders") or []
            order_id = str(orders[0].get("order_id", "")) if orders else ""

            if not order_id:
                log(f"  rejected: {orders[0] if orders else response}")
                continue

            placed += 1

            if await wait_for_fill(rest, ticker, order_id, args.fill_timeout):
                log("  filled (maker)")
            else:
                log("  no fill; cancelling")
                await rest.batch_cancel_orders([CancelOrderRequest(order_id=order_id)])
                continue

            # Exit by resting on the other side; if it does not fill we simply
            # stop and report the position rather than crossing to force it.
            exit_price = parse_price_fp(
                (await rest._request("GET", "/markets", params={"tickers": ticker}))["markets"][0][
                    "yes_ask_dollars"
                ]
            )
            exit_id = f"{PREFIX}-{int(time.time())}-{trip}-x"
            log(f"  resting SELL 1 @ ${exit_price / ONE_DOLLAR:.2f}")
            exit_response = await rest.batch_create_orders(
                [
                    CreateOrderRequest(
                        ticker=ticker,
                        side="ask",
                        price=exit_price,
                        count=1 * COUNT_SCALE,
                        client_order_id=exit_id,
                        post_only=True,
                    )
                ]
            )
            exit_orders = exit_response.get("orders") or []
            exit_order_id = str(exit_orders[0].get("order_id", "")) if exit_orders else ""

            if exit_order_id and not await wait_for_fill(
                rest, ticker, exit_order_id, args.fill_timeout
            ):
                log("  exit did not fill; cancelling and stopping")
                await rest.batch_cancel_orders([CancelOrderRequest(order_id=exit_order_id)])
                break

        # ---- what did they actually charge us ----
        after = await collect_fills(rest)
        new = [f for f in after if str(f.get("trade_id")) not in before]
        log(f"{len(new)} new fill(s) from {placed} placed order(s)")

        rows = []

        for fill in new:
            rows.append(
                {
                    "yes_price": parse_price_fp(fill.get("yes_price_dollars", "0")),
                    "count": parse_count_fp(str(fill.get("count_fp", "0"))),
                    "is_taker": bool(fill.get("is_taker")),
                    "fee_micros": parse_money_fp(str(fill.get("fees_paid_dollars", "0"))),
                    "action": fill.get("action"),
                }
            )

        out = Path(args.output)
        out.write_text(json.dumps(rows, indent=2))
        log(f"wrote {out}")

        if rows:
            print()
            print(f"{'price':>8}{'count':>8}{'taker':>7}{'fee charged':>14}{'model says':>14}")
            model = KalshiFeeModel()

            for row in rows:
                modelled = model.fee_micros(
                    yes_price=row["yes_price"],
                    count=row["count"],
                    is_taker=row["is_taker"],
                )
                print(
                    f"{row['yes_price'] / ONE_DOLLAR:>8.2f}"
                    f"{row['count'] / COUNT_SCALE:>8.0f}"
                    f"{str(row['is_taker']):>7}"
                    f"{row['fee_micros'] / MONEY_SCALE:>14.4f}"
                    f"{modelled / MONEY_SCALE:>14.4f}"
                )

            calibration = calibrate_from_fills(
                model,
                [
                    (r["yes_price"], r["count"], r["is_taker"], r["fee_micros"])
                    for r in rows
                ],
            )
            print()
            print("VERDICT:", calibration.describe())

            makers = [r for r in rows if not r["is_taker"]]

            if makers:
                charged = sum(r["fee_micros"] for r in makers)
                print(
                    f"maker fills: {len(makers)}, total charged "
                    f"${charged / MONEY_SCALE:.4f} -> "
                    + ("MAKER FEES ARE REAL" if charged > 0 else "NO MAKER FEE CHARGED")
                )
    finally:
        try:
            for ticker_name in {t for t in [locals().get("ticker")] if t}:
                for order in await rest.get_orders(ticker=ticker_name, status="resting"):
                    client_order_id = str(order.get("client_order_id", ""))

                    if client_order_id.startswith(PREFIX):
                        await rest.batch_cancel_orders(
                            [CancelOrderRequest(order_id=str(order["order_id"]))]
                        )
                        log(f"cleanup: cancelled {order['order_id']}")

                positions = await rest.get_positions((ticker_name,))
                log(f"final position {ticker_name}: {positions.get(ticker_name, 0) / COUNT_SCALE:+.0f}")

            log(f"final balance ${await rest.get_available_balance_cents() / 100:,.2f}")
        finally:
            await rest.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Actually place orders.")
    parser.add_argument("--ticker", help="Target this market instead of auto-selecting.")
    parser.add_argument("--round-trips", type=int, default=4)
    parser.add_argument("--max-loss", type=float, default=3.0, help="Abort after this loss.")
    parser.add_argument("--max-balance", type=float, default=200.0)
    parser.add_argument("--min-volume", type=float, default=5000.0)
    parser.add_argument("--max-spread", type=float, default=3.0, help="Cents.")
    parser.add_argument("--fill-timeout", type=float, default=120.0)
    parser.add_argument("--output", default="data/fee_calibration.json")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
