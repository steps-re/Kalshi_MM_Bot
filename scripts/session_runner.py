"""Run the market maker for hours, in the regime that measured positive, safely.

    python scripts/session_runner.py --hours 3 --dry-run
    python scripts/session_runner.py --hours 3 --execute --confirm-real-money

One seven-minute run is not evidence, and today's live trading lost $4.70 while
markout claimed a profit. The gap is everything markout cannot see - inventory
carried, the cost of getting out, and positions left to settle at 0 or 1. This
runner is built to close that gap: it trades only where the edge was measured,
flattens rather than settling, and reports P&L from the account rather than from
a metric.

## What it does differently from the ad-hoc runs

* **Early window only.** Live markout across 784 fills: +0.27c with six minutes
  or more left, +0.06c inside that, and negative in the 1-2 minute band. Today's
  losing sessions were deliberately concentrated in the closing minutes. Each
  cycle here starts at window open and stops with `--stop-before` seconds left.
* **Flattens every cycle.** Nineteen tickers were left to settle today, each a
  coin flip on a binary. A market maker's P&L should come from the spread, not
  from settlement, so any residual position is crossed out at the end of a cycle
  and the cost of doing so is reported rather than hidden.
* **Account truth, not metric truth.** Every cycle records the balance before
  and after. Markout is reported alongside, precisely so the two can be compared
  - that disagreement is the most informative number available.
* **Hard floor.** The session stops if the balance falls below `--min-balance`,
  checked between cycles. Unattended runs need a bound that does not depend on
  any strategy behaving correctly.

## What it deliberately does not do

It does not restart itself after a halt, and it does not size up when things go
well. Both belong to a person looking at the numbers.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import poll_record as pr  # noqa: E402

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.rest import CreateOrderRequest, KalshiRestClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402
from kalshi_mm_bot.live.journal import read_journal  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402

PINNED = ("KXBTC15M", "KXETH15M")

# Failures that mean "slow down" rather than "you are broken".
TRANSIENT_SIGNS = (
    "429",
    "too_many_requests",
    "ReadTimeout",
    "ConnectError",
    "RemoteProtocolError",
    "503",
)
TICKS_PER_CENT = ONE_DOLLAR // 100


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


@dataclass
class Cycle:
    index: int
    tickers: tuple[str, ...]
    balance_before: float
    balance_after: float
    fills: int
    markout_cents: float | None
    flattened: int

    @property
    def pnl(self) -> float:
        return self.balance_after - self.balance_before


@dataclass
class Session:
    cycles: list[Cycle] = field(default_factory=list)

    def report(self) -> str:
        if not self.cycles:
            return "no cycles completed"

        total = sum(c.pnl for c in self.cycles)
        fills = sum(c.fills for c in self.cycles)
        scored = [c.markout_cents for c in self.cycles if c.markout_cents is not None]
        lines = [
            "",
            f"{'cycle':>6}{'fills':>7}{'markout':>10}{'P&L':>9}{'flat':>6}",
        ]

        for c in self.cycles:
            mark = f"{c.markout_cents:+.3f}c" if c.markout_cents is not None else "     -"
            lines.append(
                f"{c.index:>6}{c.fills:>7}{mark:>10}{c.pnl:>+9.2f}{c.flattened:>6}"
            )

        lines.append("")
        lines.append(
            f"{len(self.cycles)} cycles, {fills} fills, "
            f"account P&L {total:+.2f}"
        )

        if scored:
            mean_mark = sum(scored) / len(scored)
            implied = mean_mark * fills / 100.0
            lines.append(
                f"mean markout {mean_mark:+.3f}c implies {implied:+.2f} of edge; "
                f"the account says {total:+.2f}."
            )
            lines.append(
                "The difference is inventory, exit cost and settlement - "
                "everything markout cannot see."
            )

        return "\n".join(lines)


async def balance(rest: KalshiRestClient) -> float:
    return await rest.get_available_balance_cents() / 100


async def flatten(rest: KalshiRestClient, tickers: tuple[str, ...]) -> int:
    """Cross out any residual position. Returns contracts closed.

    Crossing costs the taker fee and half a spread, and that is the point: a
    position left to settle is a coin flip on a binary, which is not the
    business. Paying to be flat is a cost worth measuring, not avoiding.
    """

    closed = 0
    positions = await rest.get_positions(tickers)

    for ticker, position in positions.items():
        if not position:
            continue

        book = pr.get(f"/markets/{ticker}/orderbook", {"depth": 1}).get(
            "orderbook_fp", {}
        )
        yes = book.get("yes_dollars") or []
        no = book.get("no_dollars") or []

        if not yes or not no:
            log(f"  cannot flatten {ticker}: no two-sided book")
            continue

        best_bid = max(int(round(float(p) * ONE_DOLLAR)) for p, _ in yes)
        best_ask = ONE_DOLLAR - max(int(round(float(p) * ONE_DOLLAR)) for p, _ in no)

        # `side` is the BOOK side, not the outcome. A long is closed by resting
        # an ask that crosses into the bid, and a short by a bid that crosses
        # into the ask. Passing "yes"/"no" here is rejected with
        # "side must be bid or ask", which is how the first version silently
        # left every position open - the very thing this function exists to
        # prevent.
        side = "ask" if position > 0 else "bid"
        price = best_bid if position > 0 else best_ask

        try:
            await rest.batch_create_orders(
                [
                    CreateOrderRequest(
                        ticker=ticker,
                        side=side,
                        price=price,
                        count=abs(position),
                        client_order_id=f"flat-{int(time.time() * 1000)}",
                        post_only=False,
                    )
                ]
            )
            await asyncio.sleep(2.0)
            left = (await rest.get_positions((ticker,))).get(ticker, 0)
            closed += (abs(position) - abs(left)) // COUNT_SCALE
            log(
                f"  flattened {ticker}: {position / COUNT_SCALE:+.0f} -> "
                f"{left / COUNT_SCALE:+.0f}"
            )
        except Exception as error:
            log(f"  flatten FAILED for {ticker}: {type(error).__name__} {error}")

    return closed


def live_windows(min_left: float, max_left: float) -> tuple[str, ...]:
    found = []

    for series in PINNED:
        try:
            data = pr.get(
                "/markets", {"status": "open", "limit": 5, "series_ticker": series}
            )
        except Exception:
            continue

        for market in data.get("markets", []) or []:
            bid = pr._num(market.get("yes_bid_dollars"))
            ask = pr._num(market.get("yes_ask_dollars"))

            if not 0 < bid < ask < 1:
                continue

            try:
                close = datetime.fromisoformat(
                    str(market["close_time"]).replace("Z", "+00:00")
                )
            except (KeyError, ValueError):
                continue

            left = (close - datetime.now(UTC)).total_seconds()

            if min_left <= left <= max_left:
                found.append(market["ticker"])

    return tuple(found)


def journal_markout(path: Path) -> tuple[int, float | None]:
    if not path.exists():
        return 0, None

    values = []
    fills = 0

    for event in read_journal(path):
        if event.get("event") != "filled":
            continue

        fills += 1
        mid = event.get("mid_at_fill")

        if mid is None:
            continue

        drift = (mid - event["yes_price"]) / TICKS_PER_CENT
        values.append(drift if event.get("action") == "buy" else -drift)

    return fills, (sum(values) / len(values) if values else None)


async def run(args: argparse.Namespace) -> None:
    settings = load_settings()
    environment = settings.environment(prod=True)
    rest = KalshiRestClient(
        environment.rest_base_url,
        KalshiAuth(settings.api_key_id, settings.private_key_path),
    )
    session = Session()
    journals = Path(args.journal_dir)
    journals.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + args.hours * 3600
    index = 0

    try:
        start_balance = await balance(rest)
        log(f"starting balance ${start_balance:,.2f}; floor ${args.min_balance:,.2f}")

        while time.time() < deadline:
            current = await balance(rest)

            if current < args.min_balance:
                log(f"HALT: balance ${current:,.2f} below floor ${args.min_balance:,.2f}")
                break

            tickers = live_windows(args.stop_before + 60, args.stop_before + 660)

            if not tickers:
                await asyncio.sleep(20)
                continue

            index += 1
            journal = journals / f"cycle{index:03d}.jsonl"
            duration = max(60, args.window_seconds)
            log(f"cycle {index}: {', '.join(t[-18:] for t in tickers)} for {duration}s")

            command = [
                sys.executable,
                str(ROOT / "scripts" / "live_trade.py"),
                *tickers,
                "--prod",
                "--strategy",
                args.strategy,
                "--order-size",
                str(args.order_size),
                "--max-position",
                str(args.max_position),
                "--duration-sec",
                str(duration),
                "--journal",
                str(journal),
                "--min-requote-sec",
                str(args.min_requote_sec),
            ]

            if args.execute:
                command += ["--execute", "--confirm-real-money"]

            result = subprocess.run(
                command, cwd=str(ROOT), capture_output=True, text=True
            )

            if result.returncode != 0:
                # Never swallow this. A first version captured output and
                # discarded it, and a failing child produced 90 "successful"
                # zero-fill cycles in seven minutes that looked like a quiet
                # market rather than a broken command.
                output = (result.stderr or result.stdout or "").strip()
                log(f"  live_trade exited {result.returncode}")

                for line in output.splitlines()[-4:]:
                    log(f"    {line}")

                # A rate limit or a dropped connection is the exchange telling
                # us to slow down, not a broken session. Backing off and taking
                # the next window is right; stopping a three-hour run over one
                # 429 throws away the experiment. A bad argument, by contrast,
                # will fail identically forever and must stop.
                if any(sign in output for sign in TRANSIENT_SIGNS):
                    log(f"  transient - backing off {args.backoff_seconds:.0f}s")
                    await asyncio.sleep(args.backoff_seconds)
                    continue

                log("  stopping: a session that cannot trade should not keep cycling")
                break

            closed = await flatten(rest, tickers) if args.execute else 0
            await asyncio.sleep(3)
            after = await balance(rest)
            fills, mark = journal_markout(journal)

            session.cycles.append(
                Cycle(
                    index=index,
                    tickers=tickers,
                    balance_before=current,
                    balance_after=after,
                    fills=fills,
                    markout_cents=mark,
                    flattened=closed,
                )
            )
            log(
                f"  fills {fills}  markout "
                f"{f'{mark:+.3f}c' if mark is not None else '-'}  "
                f"P&L {after - current:+.2f}  balance ${after:,.2f}"
            )
            print(session.report())
    finally:
        await rest.close()
        print(session.report())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--strategy", default="phased:adaptive")
    parser.add_argument("--order-size", default="1")
    parser.add_argument("--max-position", default="5")
    parser.add_argument(
        "--window-seconds",
        type=int,
        default=420,
        help="How long to trade in each window. Default 420s, which from window "
        "open leaves the closing minutes untraded.",
    )
    parser.add_argument(
        "--stop-before",
        type=float,
        default=420.0,
        help="Seconds of window life that must remain when a cycle ENDS. The "
        "edge measured live is +0.27c above six minutes and +0.06c below it.",
    )
    parser.add_argument("--min-balance", type=float, default=35.0)
    parser.add_argument("--backoff-seconds", type=float, default=90.0)
    parser.add_argument(
        "--min-requote-sec",
        default="2.0",
        help="Throttle requoting. The default strategy re-quotes on nearly "
        "every book tick, which reached Kalshi's order rate limit and returned "
        "429 within fourteen seconds of a session starting.",
    )
    parser.add_argument("--journal-dir", default="data/session")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-real-money", action="store_true")
    args = parser.parse_args()

    if args.execute and not args.confirm_real_money:
        parser.error("--execute requires --confirm-real-money")

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
