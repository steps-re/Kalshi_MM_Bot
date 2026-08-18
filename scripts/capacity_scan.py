"""How many markets could this strategy actually run on at once?

    python scripts/capacity_scan.py
    python scripts/capacity_scan.py --probe 200 --order-size 1

Deployment breadth is the only lever left on total profit. A live run made
$0.21 in seven minutes on one market at one contract, and that scales with the
number of markets far more safely than with size: doubling size doubles both
edge and adverse selection in the same book, while a second market is an
independent draw. So the question is not "does this work" - measured, it does,
slightly - but "how many places does it work at once".

The screen uses what has been measured rather than what sounds right. Each
market is judged on:

* **Queue reachability.** Depth resting at the touch divided by flow gives the
  wait to reach the front. Flow is measured over the market's own life, never
  over 24 hours: a 15-minute window has no 24-hour history, and a sports market
  concentrates a day's volume into two hours and looks tradable at 3am.
* **Fee viability at its price.** Makers pay nothing on this account, so a
  resting quote needs only to beat adverse selection - but a market we might
  have to cross out of still owes the taker fee, and that is priced at its own
  price rather than at the midpoint.
* **Book activity.** A market whose book barely changes cannot be market-made
  regardless of its spread. Measured on the two series that work: KXBTC15M
  carries 250-360 book deltas/sec and KXETH15M 106-180. A market two orders of
  magnitude below that is a different business.

What it deliberately does not do is rank on spread. Wide spreads in dead markets
are wide *because* they are dead, and an earlier version of this screen put
KXWNBATEAMTOTAL top of the exchange on a 48c spread over a book that trades 56
contracts a day.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import poll_record as pr  # noqa: E402

from kalshi_mm_bot.analytics.screening import parse_market  # noqa: E402
from kalshi_mm_bot.market.fees import DEFAULT_FEE_MODEL  # noqa: E402
from kalshi_mm_bot.market.price import (  # noqa: E402
    COUNT_SCALE,
    ONE_DOLLAR,
    parse_price_fp,
)

# Measured on the run that actually made money: 28 fills in seven minutes against
# a median 237 contracts resting ahead of us, so the queue there turned over in
# well under a minute.
#
# Both bounds are needed, and the first version had only the fraction. A market
# that lives 45 hours passes any fractional test with a 38,000-second queue -
# which is exactly what happened: the scan returned four dead esports books whose
# only qualification was living long enough for a hopeless queue to look
# proportionate. Reachability is an absolute property of the queue, and the
# fraction only stops us joining a queue that clears just as the market closes.
MAX_WAIT_SECONDS = 300.0

# Share of a level's shrinkage that is a real trade rather than a cancellation,
# measured off the websocket delta feed on KXBTC15M and KXETH15M. The rest of
# the shrinkage is people pulling orders - which still advances our place in the
# queue. See sim/fills.py and scripts/calibrate_fills.py.
TRADE_FRACTION = 0.163
MAX_WAIT_FRACTION_OF_LIFE = 0.25

# Minimum lifetime volume before flow means anything. Low on purpose: a
# fifteen-minute window two minutes old has traded very little in absolute terms
# and is the most tradable book on the exchange. Screening it out on volume is
# how the first run of this scan missed both markets that actually work.
MIN_VOLUME_CONTRACTS = 50.0


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker: str
    series: str
    mid: int
    spread_ticks: int
    depth: float
    volume_contracts: float
    age_seconds: float
    seconds_to_close: float

    @property
    def flow_per_second(self) -> float:
        """Contracts a second, over the market's own life, one side only."""

        return (self.volume_contracts / max(1.0, self.age_seconds)) / 2.0

    @property
    def wait_seconds(self) -> float:
        """Seconds to reach the front of the queue at our price.

        The queue ahead does not only shrink by trading. **Measured off the
        websocket feed, 84% of a level's shrinkage is cancellation**, and an
        order ahead of us that cancels advances us exactly as much as one that
        trades. Dividing depth by traded flow alone therefore overstates the
        wait by about six times.

        That is not a small correction, it is the difference between right and
        useless. The first version of this screen rated KXBTC15M unreachable -
        a 347-second wait against a 378-second life - in a market we had traded
        the same afternoon for 116 fills. Scaling by the measured trade fraction
        gives 57 seconds, which matches what actually happened.
        """

        if self.flow_per_second <= 0:
            return float("inf")

        shrinkage_per_second = self.flow_per_second / TRADE_FRACTION
        return self.depth / shrinkage_per_second

    @property
    def reachable(self) -> bool:
        """Can we get through this queue, and well before the market closes?"""

        if self.seconds_to_close <= 0:
            return False

        if self.wait_seconds > MAX_WAIT_SECONDS:
            return False

        return self.wait_seconds <= self.seconds_to_close * MAX_WAIT_FRACTION_OF_LIFE

    @property
    def cross_cost_cents(self) -> float:
        """Fee to cross one contract at this mid - the cost of a forced exit.

        0.07 x P(1-P) peaks at 0.50 and vanishes in the tails. This is the
        second condition of the passive-exit paradigm: a book that lives near
        the middle makes every unavoidable flatten expensive, which is why the
        crypto majors (near 0.50) lose to the commodity windows (tail-drifting)
        even when both are reachable.
        """

        p = self.mid / ONE_DOLLAR
        return DEFAULT_FEE_MODEL.fee_micros(
            yes_price=self.mid, count=COUNT_SCALE, is_taker=True
        ) / 10_000

    def taker_exit_cents(self, count: int) -> float:
        """What crossing out would cost, at this market's own price."""

        return (
            DEFAULT_FEE_MODEL.fee_micros(
                yes_price=self.mid, count=count, is_taker=True
            )
            / 10_000
            / (count / COUNT_SCALE)
        )


def collect(series_list: list[str], probe: int) -> list[Candidate]:
    per_series_cap = max(2, probe // max(1, len(series_list)))
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    found: list[Candidate] = []
    seen: set[str] = set()

    for series in series_list:
        try:
            data = pr.get(
                "/markets",
                {"status": "open", "limit": 200, "series_ticker": series},
            )
        except Exception as error:
            print(f"  {series}: {type(error).__name__} {error}")
            continue

        for raw in data.get("markets", []) or []:
            ticker = raw.get("ticker")

            if not ticker or ticker in seen:
                continue

            quote = parse_market(raw, now=now)

            if quote is None or not quote.is_quotable or not quote.seconds_to_close:
                continue

            volume = pr._num(raw.get("volume_fp")) / COUNT_SCALE

            if volume < MIN_VOLUME_CONTRACTS:
                continue

            open_time = raw.get("open_time")

            try:
                opened = datetime.fromisoformat(str(open_time).replace("Z", "+00:00"))
                age = max(1.0, (now - opened).total_seconds())
            except (TypeError, ValueError):
                age = 86_400.0

            seen.add(ticker)
            found.append((quote, volume, age))

            # Per series, not global: exhausting the budget on whichever series
            # is listed first means never looking at the rest.
            if len(found) >= per_series_cap * (series_list.index(series) + 1):
                break

    # Depth needs a book call each, so it happens after the cheap filters.
    candidates: list[Candidate] = []

    for quote, volume, age in found:
        try:
            book = pr.get(
                f"/markets/{quote.ticker}/orderbook", {"depth": 2}
            ).get("orderbook_fp", {})
        except Exception:
            continue

        yes = book.get("yes_dollars") or []
        no = book.get("no_dollars") or []

        if not yes or not no:
            continue

        bids = {parse_price_fp(p): float(s) for p, s in yes}
        asks = {ONE_DOLLAR - parse_price_fp(p): float(s) for p, s in no}
        best_bid, best_ask = max(bids), min(asks)

        if best_bid >= best_ask:
            continue

        candidates.append(
            Candidate(
                ticker=quote.ticker,
                series=quote.series or quote.ticker.split("-")[0],
                mid=(best_bid + best_ask) // 2,
                spread_ticks=best_ask - best_bid,
                depth=(bids[best_bid] + asks[best_ask]) / 2,
                volume_contracts=volume,
                age_seconds=age,
                seconds_to_close=quote.seconds_to_close,
            )
        )

    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", type=int, default=120)
    parser.add_argument("--order-size", type=int, default=1)
    parser.add_argument(
        "--series",
        nargs="+",
        default=[
            "KXBTC15M", "KXETH15M", "KXBTCD", "KXETHD",
            "KXNBAGAME", "KXNFLGAME", "KXMLBGAME", "KXNHLGAME",
            "KXVALORANTGAME", "KXCS2GAME", "KXLOLGAME",
            "KXTRUMPSAY", "KXHIGHNY", "KXHIGHCHI", "KXINXU", "KXNASDAQ100U",
        ],
    )
    args = parser.parse_args()

    print(f"probing up to {args.probe} markets across {len(args.series)} series")
    candidates = collect(args.series, args.probe)
    reachable = [c for c in candidates if c.reachable]

    print(f"{len(candidates)} two-sided books; {len(reachable)} with a reachable queue")
    print()
    print(
        f"{'ticker':<30}{'mid':>7}{'spr':>6}{'depth':>9}"
        f"{'flow/s':>9}{'wait':>9}{'life':>8}{'cross$':>10}"
    )

    count = args.order_size * COUNT_SCALE

    # Ranked by the passive-exit paradigm: among reachable books (condition 1),
    # cheapest forced-cross first (condition 2). A reachable, tail-priced book is
    # the target; a reachable near-0.50 book is penalised because its unavoidable
    # flattens are dear.
    for c in sorted(reachable, key=lambda c: c.cross_cost_cents)[:20]:
        flag = "  <- near 0.50, costly exits" if c.cross_cost_cents > 1.0 else ""
        print(
            f"{c.ticker[-29:]:<30}{c.mid / ONE_DOLLAR:>7.2f}"
            f"{c.spread_ticks / 100:>5.1f}c{c.depth:>9,.0f}"
            f"{c.flow_per_second:>9.1f}{c.wait_seconds:>8.0f}s"
            f"{c.seconds_to_close / 60:>7.0f}m{c.cross_cost_cents:>9.2f}c{flag}"
        )

    print()
    by_series: dict[str, int] = {}

    for c in reachable:
        by_series[c.series] = by_series.get(c.series, 0) + 1

    print("simultaneously deployable, by series:")

    for series, n in sorted(by_series.items(), key=lambda kv: -kv[1]):
        print(f"  {series:<24}{n:>4}")

    print()
    print(f"TOTAL simultaneous capacity right now: {len(reachable)} market(s)")


if __name__ == "__main__":
    main()
