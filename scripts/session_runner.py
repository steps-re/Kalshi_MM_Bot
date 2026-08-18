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
import json
import re
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
from kalshi_mm_bot.api.rest import (  # noqa: E402
    CancelOrderRequest,
    CreateOrderRequest,
    KalshiRestClient,
)
from kalshi_mm_bot.config import load_settings  # noqa: E402
from kalshi_mm_bot.live.journal import read_journal  # noqa: E402
from kalshi_mm_bot.market.bookio import rest_top  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402

# Ranked by simulated per-venue markout (2026-08-18, 11 recordings): the
# commodity 15-minute windows out-rank the crypto majors 2-4x, because BTC/ETH
# are the thickest, most-competed books and our resting quotes sit behind the
# most informed flow. WTI/silver/gold/doge are less picked-over. Order here is
# the trading priority; the session takes the top --max-books that have a live
# window this cycle. Sim ranking is a hypothesis - live fills re-rank it.
# Re-ranked by LIVE markout after one session (2026-08-18), which confirmed the
# sim ranking's shape and named the drag: ETH marked -0.52c/56% on 72 real
# fills - the single loser, and the thick book we had been anchored to - so it
# is dropped. BTC survives (positive +0.36c live) but sits last: it is thick,
# competed, and its 3.6% fill rate means quotes rarely reach the front, so the
# book cap will seldom get to it once thinner windows are open.
DEFAULT_FAMILY = (
    "KXWTI15M",     # live +0.75c / 75%
    "KXSOL15M",     # live +0.55c / 79%
    "KXSILVER15M",  # live +0.53c / 79%
    "KXDOGE15M",    # live +0.52c / 71%
    "KXGOLD15M",    # live +0.39c / 67%
    "KXXRP15M",     # live +0.17c / 64% - marginal, kept for now
    "KXBTC15M",     # live +0.36c / 71% but thick, low fill-rate; last resort
)

# Failures a long run must NOT survive: they will fail identically forever, so
# retrying is just burning the session quietly. Everything else is treated as
# transient and backed off.
#
# An allowlist of transient signs was tried first and was wrong in exactly the
# way an allowlist always is: it listed ConnectError but not
# ConnectionClosedError, so a dropped websocket ended a three-hour experiment
# after twenty-five minutes. The failures that should stop a session are few and
# knowable; the ways a network can break are not.
PERMANENT_SIGNS = (
    "error: argument",          # argparse rejected the command
    "error: unrecognized",
    "invalid choice",
    # 401 is permanent ONLY when it is actually about credentials. A
    # header_timestamp_expired 401 is a clock artifact - the laptop slept
    # mid-session and the first post-wake request carried a pre-sleep
    # timestamp - and it self-heals on the next request. Matching bare "401"
    # stopped a six-hour run three cycles in.
    "invalid_api_key",
    "api key not found",
    "403 Forbidden",
    "ModuleNotFoundError",
    "ImportError",
    "unknown strategy",
)

# Even so, something unrecognised must not spin forever.
MAX_CONSECUTIVE_FAILURES = 5
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
    arm: str = ""

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


async def flatten(
    rest: KalshiRestClient,
    tickers: tuple[str, ...],
    settle_band_ticks: int = 300,
    settle_max_contracts: int = 2,
    rest_exit_seconds: float = 45.0,
) -> int:
    """Close residual positions, choosing the cheaper of crossing and settling.

    Crossing out costs half a spread plus the taker fee, every cycle. Settling
    costs nothing but carries the binary's remaining variance. Near the ends of
    the price range that variance is pennies while the crossing cost is not, so
    inside `settle_band_ticks` of 0 or 1 the position is left to settle and the
    saving is logged. Everywhere else it is crossed, because a mid-priced binary
    held to settlement is a coin flip, which is not the business.

    Nineteen positions were once left to settle by accident and dominated the
    day's P&L; the band makes the same choice deliberate, bounded, and only
    where the lottery ticket is nearly worthless or nearly certain.

    Returns contracts closed by crossing.
    """

    closed = 0
    positions = await rest.get_positions(tickers)

    for ticker, position in positions.items():
        if not position:
            continue

        # Everything per-ticker sits inside its own try: the book fetch used to
        # sit outside it, so one 429 on ticker #1 abandoned ticker #2 entirely,
        # with the failure logged as a balance-read problem. Rate limits are
        # exactly what flattening at window close hits.
        try:
            top = rest_top(
                pr.get(f"/markets/{ticker}/orderbook", {"depth": 3}).get(
                    "orderbook_fp", {}
                )
            )
        except Exception as error:
            log(
                f"  UNFLATTENED {ticker} ({position / COUNT_SCALE:+.0f}): "
                f"book fetch failed ({type(error).__name__}) - position rides"
            )
            continue

        if top is None:
            # rest_top is None for one-sided/crossed books too, which is common
            # in a window's last seconds - precisely when this runs. That is an
            # unflattened position, not a benign skip, and it must read as one.
            log(
                f"  UNFLATTENED {ticker} ({position / COUNT_SCALE:+.0f}): "
                "book empty/one-sided - will settle unchecked"
            )
            continue

        near_certain = (
            top.mid <= settle_band_ticks or top.mid >= ONE_DOLLAR - settle_band_ticks
        )

        # The band bounds VARIANCE only when size is small: at a 3c mid the
        # per-contract settlement sd is sqrt(.03*.97) ~ 17c, an order of
        # magnitude above the 1-2c crossing cost. Cheap per contract, ruinous
        # per two hundred - so the settle path is size-capped and everything
        # larger crosses regardless of price.
        if near_certain and abs(position) <= settle_max_contracts * COUNT_SCALE:
            log(
                f"  leaving {ticker} ({position / COUNT_SCALE:+.0f}) to settle: "
                f"mid {top.mid / ONE_DOLLAR:.2f} is inside the certainty band, "
                "crossing would pay real cost to remove negligible variance"
            )
            continue

        # Exit passively FIRST. The account truth this whole runner exists to
        # measure said the quiet part out loud: 1,621 maker fills cost $0.01 in
        # fees while 107 taker fills - almost all of them these flattens - cost
        # $1.02, and that taker drag exceeded the entire paper markout edge.
        # Crossing to flatten was handing back the edge we collected for free.
        #
        # We stop trading with ~7 minutes of window left, so a resting exit at
        # the touch has time to fill as a maker (zero fee). Only the stub that
        # has not rested by the deadline is crossed.
        rest_side = "ask" if position > 0 else "bid"
        rest_price = top.ask if position > 0 else top.bid  # join the touch we exit into

        try:
            await rest.batch_create_orders(
                [
                    CreateOrderRequest(
                        ticker=ticker,
                        side=rest_side,
                        price=rest_price,
                        count=abs(position),
                        client_order_id=f"rflat-{int(time.time() * 1000)}",
                        post_only=True,
                    )
                ]
            )
        except Exception as error:
            log(f"  resting exit rejected for {ticker} ({type(error).__name__})")

        await asyncio.sleep(rest_exit_seconds)
        remaining = (await rest.get_positions((ticker,))).get(ticker, 0)

        if remaining == 0:
            log(f"  exited {ticker} passively (free): {position / COUNT_SCALE:+.0f} -> 0")
            continue

        # Cancel the unfilled resting exit before crossing the stub, or we would
        # hold two opposing orders.
        try:
            resting = await rest.get_orders(ticker=ticker, status="resting")
            stale = [
                str(o.get("order_id"))
                for o in resting
                if str(o.get("client_order_id", "")).startswith("rflat-")
            ]
            if stale:
                await rest.batch_cancel_orders(
                    [CancelOrderRequest(order_id=i) for i in stale]
                )
        except Exception as error:
            log(f"  could not cancel resting exit for {ticker} ({type(error).__name__})")

        # Cross the remainder. `side` is the BOOK side: a long is closed by an
        # ask crossing into the bid, a short by a bid crossing into the ask.
        side = "ask" if remaining > 0 else "bid"
        price = top.bid if remaining > 0 else top.ask

        try:
            await rest.batch_create_orders(
                [
                    CreateOrderRequest(
                        ticker=ticker,
                        side=side,
                        price=price,
                        count=abs(remaining),
                        client_order_id=f"flat-{int(time.time() * 1000)}",
                        post_only=False,
                    )
                ]
            )
            await asyncio.sleep(2.0)
            left = (await rest.get_positions((ticker,))).get(ticker, 0)
            closed += (abs(remaining) - abs(left)) // COUNT_SCALE
            log(
                f"  crossed stub {ticker}: {remaining / COUNT_SCALE:+.0f} -> "
                f"{left / COUNT_SCALE:+.0f} (passive exit left {(position - remaining) / COUNT_SCALE:+.0f})"
            )
        except Exception as error:
            log(f"  flatten FAILED for {ticker}: {type(error).__name__} {error}")

    return closed


def live_windows(
    min_left: float,
    max_left: float,
    family: tuple[str, ...],
    max_books: int,
) -> tuple[str, ...]:
    """The first `max_books` series (in priority order) with a live window.

    Priority order matters: capping simultaneous books is the rate-limit
    guardrail - eight books re-quoting at once is what 429'd earlier sessions -
    so the cap must take the BEST-ranked windows, not the first ones the API
    happens to return.
    """

    found = []

    for series in family:
        if len(found) >= max_books:
            break

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
                break  # one window per series - the currently-open one

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


# Exit codes let the supervisor distinguish "the run finished, start another"
# from "a safety limit tripped, stay down". systemd restarts on DEADLINE (and
# on a crash) but never on FLOOR or PERMANENT - see the unit's
# RestartPreventExitStatus. Without this, a floor halt and a 12h deadline both
# exit 0 and auto-restart would drive a drained account straight back into the
# floor every 30 seconds.
EXIT_DEADLINE = 0
EXIT_FLOOR = 10
EXIT_PERMANENT = 11


async def run(args: argparse.Namespace) -> str:
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
    failures = 0
    stop_reason = "deadline"  # overwritten only by a floor halt or permanent stop
    if args.ab_arms:
        arm_specs = [a.strip() for a in args.ab_arms.split(";") if a.strip()]
    elif args.ab_edges:
        arm_specs = [
            f"min_profit_edge={e.strip()}" for e in args.ab_edges.split(",") if e.strip()
        ]
    else:
        arm_specs = []

    ab_ledger = journals / "ab_ledger.jsonl"

    if arm_specs:
        log(f"A/B arms {arm_specs}, round-robin per cycle -> {ab_ledger}")

    try:
        start_balance = await balance(rest)
        log(f"starting balance ${start_balance:,.2f}; floor ${args.min_balance:,.2f}")

        while time.time() < deadline:
            # The runner's own API calls (balance, window discovery, flatten)
            # get the same transient treatment as the child process. The first
            # version handled child failures carefully and then died itself on
            # an httpx.ConnectTimeout raised from this very loop - the
            # supervisor was the least supervised part of the system.
            try:
                current = await balance(rest)

                if current < args.min_balance:
                    log(
                        f"HALT: balance ${current:,.2f} below floor "
                        f"${args.min_balance:,.2f}"
                    )
                    stop_reason = "floor"
                    break

                tickers = live_windows(
                    args.stop_before + 60,
                    args.stop_before + 660,
                    tuple(args.series),
                    args.max_books,
                )
            except Exception as error:
                failures += 1

                if failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"  {failures} consecutive runner failures - stopping")
                    stop_reason = "permanent"
                    break

                log(
                    f"  runner network error ({type(error).__name__}), "
                    f"backing off {args.backoff_seconds:.0f}s"
                )
                await asyncio.sleep(args.backoff_seconds)
                continue

            if not tickers:
                await asyncio.sleep(20)
                continue

            index += 1
            journal = journals / f"cycle{index:03d}.jsonl"
            duration = max(60, args.window_seconds)

            # Round-robin the A/B arm by cycle so each arm sees the same venue
            # basket and differs only in regime, which the rotation balances.
            if arm_specs:
                spec = arm_specs[(index - 1) % len(arm_specs)]
                # Filename/ledger-safe label (no '_', '=', ',' - the journal name
                # and the slicer's arm parser both split on '_').
                arm_label = re.sub(r"[^A-Za-z0-9]", "", spec) or "arm"
                arm_params = []

                for kv in spec.split(","):
                    if kv.strip():
                        arm_params += ["--adaptive-param", kv.strip()]
            else:
                arm_label = "default"
                arm_params = []

            log(
                f"cycle {index} [{arm_label}]: "
                f"{', '.join(t[-18:] for t in tickers)} for {duration}s"
            )

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
                *arm_params,
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
                if any(sign in output for sign in PERMANENT_SIGNS):
                    log("  permanent failure - stopping")
                    stop_reason = "permanent"
                    break

                failures += 1

                if failures >= MAX_CONSECUTIVE_FAILURES:
                    log(f"  {failures} consecutive failures - stopping")
                    stop_reason = "permanent"
                    break

                log(
                    f"  transient ({failures}/{MAX_CONSECUTIVE_FAILURES}) - "
                    f"backing off {args.backoff_seconds:.0f}s"
                )
                await asyncio.sleep(args.backoff_seconds)
                continue

            failures = 0

            try:
                closed = (
                    await flatten(rest, tickers, args.settle_band_ticks)
                    if args.execute
                    else 0
                )
                await asyncio.sleep(3)
                after = await balance(rest)
            except Exception as error:
                # A failed post-cycle read must not kill the session; record the
                # cycle with what we know and let the floor check next loop
                # catch any real damage.
                log(f"  post-cycle error ({type(error).__name__}) - recording cycle without balance")
                closed, after = 0, current
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
                    arm=arm_label,
                )
            )
            log(
                f"  fills {fills}  markout "
                f"{f'{mark:+.3f}c' if mark is not None else '-'}  "
                f"P&L {after - current:+.2f}  balance ${after:,.2f}"
            )

            # Durable A/B ledger: one row per cycle, appended so it survives the
            # 12h restart. This, not the overwritten per-cycle journals, is what
            # ab_report.py reads to compare arms across days.
            try:
                with ab_ledger.open("a") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                                "index": index,
                                "arm": arm_label,
                                "tickers": list(tickers),
                                "fills": fills,
                                "markout_cents": mark,
                                "pnl": round(after - current, 4),
                                "balance": round(after, 4),
                            }
                        )
                        + "\n"
                    )
            except OSError as error:
                log(f"  A/B ledger write failed ({type(error).__name__}) - cycle still recorded")

            # Preserve the per-fill journal off-box before the next session
            # overwrites cycle{index}.jsonl. This is the ONLY record of mid-at-fill
            # and queue depth per fill - the data the markout slicer needs to find
            # where adverse selection bites (by queue position, price, time-in-
            # cycle). Losing it on every 12h restart was silently discarding the
            # calibration set. Unique name = arm + timestamp so restarts accumulate.
            if args.journal_bucket and journal.exists():
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                dest = f"gs://{args.journal_bucket}/journals/jrnl_{stamp}_{arm_label}.jsonl"
                try:
                    subprocess.run(
                        ["gcloud", "storage", "cp", str(journal), dest],
                        check=True, capture_output=True, timeout=60,
                    )
                except (subprocess.SubprocessError, OSError) as error:
                    log(f"  journal upload failed ({type(error).__name__}) - kept on disk")

            print(session.report())
    finally:
        await rest.close()
        print(session.report())

    return stop_reason


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--strategy", default="phased:adaptive")
    parser.add_argument(
        "--journal-bucket",
        default="",
        help=(
            "GCS bucket to preserve each cycle's per-fill journal to before the "
            "next session overwrites it. Empty = keep on disk only. These journals "
            "are the calibration set for markout_slices.py (queue/price/time toxicity)."
        ),
    )
    parser.add_argument(
        "--ab-edges",
        default="",
        help=(
            "Shortcut for an A/B over min_profit_edge, e.g. '25,75,150'. Superseded "
            "by --ab-arms when that is set."
        ),
    )
    parser.add_argument(
        "--ab-arms",
        default="",
        help=(
            "General A/B: semicolon-separated arms, each a comma-separated set of "
            "adaptive key=value overrides, e.g. 'obi_skew=0;obi_skew=42;obi_skew=85'. "
            "Each cycle round-robins to the next arm (all books that cycle share it), "
            "so every arm sees the same venue basket and only the regime differs, "
            "which the rotation balances. This is how the microprice (obi_skew) center "
            "is measured head-to-head against the mid-centered default on live P&L."
        ),
    )
    parser.add_argument(
        "--series",
        nargs="+",
        default=list(DEFAULT_FAMILY),
        help="15-minute series to trade, in priority order. Default is the full "
        "family ranked by simulated markout, commodities first.",
    )
    parser.add_argument(
        "--max-books",
        type=int,
        default=4,
        help="Max simultaneous books per cycle. The rate-limit guardrail: eight "
        "books re-quoting at once 429'd earlier runs; four at a 2s requote floor "
        "sits comfortably inside the ~4 orders/s limit.",
    )
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
        "--settle-band-ticks",
        type=int,
        default=300,
        help="Leave residual positions to settle when the mid is within this "
        "many ticks of 0 or 1 (300 = 3 cents). Crossing there pays real cost "
        "to remove negligible variance.",
    )
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

    reason = asyncio.run(run(args))
    sys.exit({"floor": EXIT_FLOOR, "permanent": EXIT_PERMANENT}.get(reason, EXIT_DEADLINE))


if __name__ == "__main__":
    main()
