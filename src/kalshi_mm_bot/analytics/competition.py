"""Detecting other market makers, and being detected by them.

A market-making edge is not a secret formula, it is a position in a queue. The
moment someone else quotes the same market a tick better, the edge moves to
them. So the questions that matter operationally are not "is the strategy
good?" but:

* Is someone stepping inside our quotes, and how fast?
* Did the spread we were counting on collapse once we started quoting it?
* Are our losing fills clustered, which is what being hunted looks like?

All three are measurable from the order book plus our own order timestamps, and
none of them require knowing who the counterparty is.

A note on the third one. Adverse selection and being *targeted* look similar in
aggregate and different in distribution. Ordinary adverse selection is diffuse:
a slightly negative markout spread across many fills. Being picked off by a
faster participant is concentrated: most fills fine, a few very bad, clustered
in time and usually in the same market. `toxicity_concentration` measures which
shape the losses have, because the responses differ - widen for the first, stop
quoting that market for the second.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import fmean, median

from kalshi_mm_bot.market.price import ONE_DOLLAR
from kalshi_mm_bot.sim.fills import SimulatedFill


@dataclass(frozen=True, slots=True)
class QuoteEpisode:
    """One period during which we were resting a quote on one side."""

    market_ticker: str
    side: str
    our_price: int
    started_at: float
    ended_at: float
    best_at_start: int
    best_at_end: int
    undercut_at: float | None = None

    @property
    def duration(self) -> float:
        return max(0.0, self.ended_at - self.started_at)

    @property
    def was_undercut(self) -> bool:
        return self.undercut_at is not None

    @property
    def seconds_before_undercut(self) -> float | None:
        if self.undercut_at is None:
            return None

        return max(0.0, self.undercut_at - self.started_at)

    @property
    def touch_moved_against_us(self) -> int:
        """How far the touch moved past our price while we rested.

        Positive means the market improved beyond us - someone was willing to
        quote tighter, which is the signature of a competitor rather than of
        ordinary drift.
        """

        if self.side == "bid":
            return max(0, self.best_at_end - self.our_price)

        return max(0, self.our_price - self.best_at_end)


@dataclass(frozen=True, slots=True)
class CompetitionReport:
    episodes: int
    undercut_episodes: int
    median_seconds_to_undercut: float | None
    median_touch_move_ticks: float
    markets_with_competition: tuple[str, ...]

    @property
    def undercut_rate(self) -> float:
        return self.undercut_episodes / self.episodes if self.episodes else 0.0

    def describe(self) -> str:
        if not self.episodes:
            return "competition: no quote episodes recorded"

        lines = [
            "competition:",
            f"  quote episodes            {self.episodes}",
            f"  undercut by someone       {self.undercut_rate:.0%}",
        ]

        if self.median_seconds_to_undercut is not None:
            lines.append(
                f"  median time to undercut   {self.median_seconds_to_undercut:.1f}s"
            )

        lines.append(
            f"  median touch move past us {self.median_touch_move_ticks / 100:.2f}c"
        )

        if self.undercut_rate > 0.5:
            lines.append(
                "  -> a majority of quotes are being stepped inside. The spread "
                "this market was screened on is not the spread we will capture."
            )

        if self.markets_with_competition:
            shown = ", ".join(self.markets_with_competition[:5])
            lines.append(f"  most contested            {shown}")

        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class ToxicityReport:
    """Whether losses are diffuse (adverse selection) or clustered (hunted)."""

    fill_count: int
    losing_fills: int
    loss_share_from_worst_decile: float
    worst_markets: tuple[str, ...]

    @property
    def is_concentrated(self) -> bool:
        """True when a handful of fills carry most of the damage."""

        return self.fill_count >= 20 and self.loss_share_from_worst_decile > 0.5

    def describe(self) -> str:
        if not self.fill_count:
            return "toxicity: no fills to analyse"

        lines = [
            "toxicity:",
            f"  fills                     {self.fill_count}",
            f"  fills that went against   {self.losing_fills / self.fill_count:.0%}",
            f"  losses from worst 10%     {self.loss_share_from_worst_decile:.0%}",
        ]

        if self.is_concentrated:
            lines.append(
                "  -> CONCENTRATED. A few fills carry the damage, which looks "
                "like being picked off rather than ordinary adverse selection. "
                "Stop quoting the named markets before widening everywhere."
            )
        else:
            lines.append(
                "  -> diffuse. This is ordinary adverse selection; widen the "
                "quote rather than dropping markets."
            )

        if self.worst_markets:
            lines.append(f"  worst markets             {', '.join(self.worst_markets[:5])}")

        return "\n".join(lines)


def analyse_competition(episodes: Sequence[QuoteEpisode]) -> CompetitionReport:
    """Summarise how often and how quickly our quotes get stepped inside."""

    if not episodes:
        return CompetitionReport(0, 0, None, 0.0, ())

    undercut = [e for e in episodes if e.was_undercut]
    times = [
        e.seconds_before_undercut for e in undercut if e.seconds_before_undercut is not None
    ]
    moves = [e.touch_moved_against_us for e in episodes]

    by_market: dict[str, int] = {}
    for episode in undercut:
        by_market[episode.market_ticker] = by_market.get(episode.market_ticker, 0) + 1

    contested = tuple(
        ticker for ticker, _ in sorted(by_market.items(), key=lambda kv: -kv[1])
    )

    return CompetitionReport(
        episodes=len(episodes),
        undercut_episodes=len(undercut),
        median_seconds_to_undercut=median(times) if times else None,
        median_touch_move_ticks=median(moves) if moves else 0.0,
        markets_with_competition=contested,
    )


def analyse_toxicity(
    fills: Iterable[SimulatedFill],
    forward_mid: dict[str, int],
) -> ToxicityReport:
    """Split fill losses into diffuse versus concentrated.

    `forward_mid` maps fill_id to the mid some horizon after the fill; build it
    from the markout machinery so both views agree on what "after" means.
    """

    losses: list[tuple[float, str]] = []
    counted = 0

    for fill in fills:
        future = forward_mid.get(fill.fill_id)

        if future is None:
            continue

        counted += 1
        direction = 1 if fill.action == "buy" else -1
        pnl = direction * (future - fill.yes_price) * fill.count / ONE_DOLLAR

        if pnl < 0:
            losses.append((pnl, fill.market_ticker))

    if not counted or not losses:
        return ToxicityReport(counted, 0, 0.0, ())

    losses.sort()
    decile = max(1, len(losses) // 10)
    total_loss = sum(pnl for pnl, _ in losses)
    worst_loss = sum(pnl for pnl, _ in losses[:decile])

    by_market: dict[str, float] = {}
    for pnl, ticker in losses:
        by_market[ticker] = by_market.get(ticker, 0.0) + pnl

    worst_markets = tuple(
        ticker for ticker, _ in sorted(by_market.items(), key=lambda kv: kv[1])
    )

    return ToxicityReport(
        fill_count=counted,
        losing_fills=len(losses),
        loss_share_from_worst_decile=(worst_loss / total_loss) if total_loss else 0.0,
        worst_markets=worst_markets,
    )


def spread_impact(
    before: Sequence[int],
    during: Sequence[int],
) -> tuple[float, str]:
    """Did the spread narrow once we started quoting?

    A market screened at 8c that trades at 3c the moment we show up was never an
    8c opportunity. This compares the quoted spread in windows where we were
    absent against windows where we were resting.
    """

    if not before or not during:
        return 0.0, "not enough observations on both sides to compare"

    quiet = fmean(before)
    active = fmean(during)

    if quiet <= 0:
        return 0.0, "no spread observed while we were absent"

    change = (active - quiet) / quiet
    verdict = (
        "the spread we screened on survives our presence"
        if change > -0.15
        else "the spread collapses when we quote - the screen overstated this market"
    )

    return change, verdict
