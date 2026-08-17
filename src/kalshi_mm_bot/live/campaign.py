"""Guards on the premises a campaign rests on, not on its mechanics.

`live/risk.py` answers "is this session behaving safely right now" - position
size, drawdown, order rate, feed silence. All of it is true regardless of why we
are trading. This module answers a different question: **are the facts that made
this strategy worth running still facts?**

The distinction matters because the dangerous failure is not a crash. It is a
campaign that keeps running correctly and profitably-looking after the thing it
depends on has quietly changed. Concretely, this strategy exists because:

* Resting fills are charged **no fee** while crossing fills pay ~1.3c. Measured,
  not assumed. If Kalshi changes that schedule, every quote we place goes from
  free to costing more than the tick it is trying to capture, and nothing in the
  order flow would look different.
* Resting fills earn more than they lose. Measured markout, which drifts as
  other participants get faster or better informed.
* We reach the front of the queue at all. That is a property of other people's
  behaviour, not ours, and it can be competed away.

None of those are things we control, and all of them can change between one
session and the next.

## Fail closed, always

Every tripwire returns OK, TRIPPED, PENDING or UNKNOWN, and **UNKNOWN halts**.

That rule is not defensive-programming boilerplate; it is the direct lesson of
the worst bug in this project. The fee reader asked for a field Kalshi had
renamed and defaulted the miss to zero, so every fill read as free and the
monitor would have cheerfully confirmed the campaign's central premise using a
reader that was incapable of reporting anything else. A monitor that treats
"cannot measure" as "fine" is worse than no monitor, because it manufactures
confidence.

PENDING exists for the honest early case - we have not traded enough yet to say.
It does not halt, but it is **time-bounded**: a campaign that has been running
for an hour and still cannot measure its own fees is not warming up, it is
broken, so PENDING becomes UNKNOWN and the campaign stops. You do not get to
stay unmeasured forever while spending money.

## Conditions also change in our favour

A monitor that only ever brakes is half a monitor. Exchanges push liquidity
where they want it: maker rebates, fee holidays, volume incentives, promoted
series. Those are usually temporary and usually unannounced to anyone not
watching their own ledger, and the same reader that catches a fee schedule
turning against us catches one turning towards us.

So tripwires can also return FAVOURABLE. It never halts and never changes size
on its own - it says "the premise moved in our favour, by this much, and here is
the evidence." Scaling into a promotion is a decision with real downside if the
promotion ends mid-position, so it stays with a person. But nobody can act on a
window they never knew was open.

The asymmetry to keep in mind: an adverse change should stop us immediately and
a favourable one should not start us immediately. Being slow to exploit costs
opportunity; being slow to stop costs money.

## What this deliberately does not do

It does not decide position size, and it does not restart anything. It reports a
verdict and a reason. Anything that can turn trading back on after a halt is a
decision for a person, because every automated recovery this project could have
written would have been a way to resume trading through the exact conditions the
halt was built to catch.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum

from kalshi_mm_bot.market.price import MONEY_SCALE, ONE_DOLLAR

TICKS_PER_CENT = ONE_DOLLAR // 100

# A campaign may be un-measurable for this long before that itself is the
# problem. Long enough to cover a quiet stretch, short enough that a broken
# reader cannot survive a session.
DEFAULT_PENDING_GRACE_SECONDS = 3600.0


class Verdict(str, Enum):
    OK = "ok"
    TRIPPED = "tripped"
    # The premise moved in our favour by enough to be worth a person's
    # attention. Never halts, never resizes anything on its own.
    FAVOURABLE = "favourable"
    # Not enough evidence yet, and it is early enough that this is expected.
    PENDING = "pending"
    # Cannot evaluate. Treated as a halt.
    UNKNOWN = "unknown"

    @property
    def halts(self) -> bool:
        return self in {Verdict.TRIPPED, Verdict.UNKNOWN}

    @property
    def is_opportunity(self) -> bool:
        return self is Verdict.FAVOURABLE


@dataclass(frozen=True, slots=True)
class Fill:
    """One execution, as the monitor needs to see it.

    `fee_micros` is deliberately optional and must stay that way. None means the
    payload did not report a fee, which is a measurement failure and never a
    zero - see the module docstring.
    """

    yes_price: int
    count: int
    is_taker: bool
    fee_micros: int | None
    # Mid at fill time; None when the book was not captured.
    mid_at_fill: int | None = None
    # "buy" or "sell". Required for markout to have the right sign; defaulting
    # it would silently report every sell backwards.
    action: str = "buy"

    @property
    def markout_cents(self) -> float | None:
        """Signed edge in cents. Positive is in our favour.

        Direction matters and is not optional. A buy wants the mid to rise
        after the fill and a sell wants it to fall, so the same price move is
        good for one and bad for the other. An earlier version hardcoded the buy
        convention because every trial happened to be a buy; the first sell to
        reach it would have been scored exactly backwards, and a strategy
        quoting both sides produces those constantly.
        """

        if self.mid_at_fill is None:
            return None

        drift = (self.mid_at_fill - self.yes_price) / TICKS_PER_CENT
        return drift if self.action == "buy" else -drift


@dataclass(frozen=True, slots=True)
class CampaignSample:
    """Everything the monitor looks at, for one evaluation."""

    fills: Sequence[Fill]
    balance_micros: int | None
    realized_pnl_micros: int | None
    elapsed_seconds: float
    quotes_placed: int = 0

    @property
    def maker_fills(self) -> list[Fill]:
        return [f for f in self.fills if not f.is_taker]

    @property
    def taker_fills(self) -> list[Fill]:
        return [f for f in self.fills if f.is_taker]


@dataclass(frozen=True, slots=True)
class Reading:
    key: str
    verdict: Verdict
    detail: str

    def describe(self) -> str:
        marker = {
            Verdict.OK: "  ",
            Verdict.PENDING: "..",
            Verdict.TRIPPED: "!!",
            Verdict.UNKNOWN: "??",
            Verdict.FAVOURABLE: "++",
        }[self.verdict]
        return f"{marker} {self.key:<22} {self.verdict.value:<9} {self.detail}"


@dataclass(frozen=True, slots=True)
class CampaignVerdict:
    readings: tuple[Reading, ...]

    @property
    def halting(self) -> tuple[Reading, ...]:
        return tuple(r for r in self.readings if r.verdict.halts)

    @property
    def opportunities(self) -> tuple[Reading, ...]:
        return tuple(r for r in self.readings if r.verdict.is_opportunity)

    @property
    def should_halt(self) -> bool:
        return bool(self.halting)

    def describe(self) -> str:
        lines = [r.describe() for r in self.readings]

        if self.should_halt:
            reasons = ", ".join(r.key for r in self.halting)
            lines.append(f"\nHALT: {reasons}")
        else:
            lines.append("\nrunning: all premises still hold")

        # Reported after the halt line, and never instead of it: a rebate does
        # not make a broken premise acceptable.
        if self.opportunities:
            keys = ", ".join(r.key for r in self.opportunities)
            lines.append(
                f"OPPORTUNITY: {keys} - moved in our favour. Scaling into this is "
                "a decision for a person, and it will not last."
            )

        return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class CampaignLimits:
    """Thresholds. Defaults are set from what this account actually measured."""

    # Mean maker fee per fill, above which the strategy stops working. NOT zero,
    # deliberately. Across 41 measured maker fills, 40 were free and one at
    # $0.913 was charged $0.000050 - five millionths of a dollar, about 1% of
    # what a taker would have paid there, against a median markout of +0.20c.
    # A strict-zero threshold would have halted a healthy campaign over dust,
    # and a monitor that cries wolf gets switched off. So the bar is economic:
    # trip when the fee is a meaningful fraction of the edge, and report any
    # departure from free as an observation either way.
    max_mean_maker_fee_micros: int = 500  # 0.05c per fill
    # Any maker fee at all is still a change worth knowing about, even when it
    # is too small to trip.
    notable_maker_fee_micros: int = 1
    # Below this, a resting quote is losing money on average.
    min_mean_markout_cents: float = -0.25
    # Above this, something has changed for the better - a promotion pulling in
    # uninformed flow, a competitor withdrawing - and it is worth knowing while
    # it lasts. Measured baseline is a mean near zero with a +0.20c median.
    good_mean_markout_cents: float = 0.75
    # Queue crowding shows up here before it shows up in P&L.
    min_fill_rate: float = 0.05
    # Session floor. Separate from RiskLimits' drawdown: this is absolute.
    min_balance_micros: int | None = None
    max_session_loss_micros: int | None = None
    # Below these counts a tripwire cannot speak, and says PENDING not OK.
    min_maker_fills: int = 10
    min_markout_fills: int = 20
    pending_grace_seconds: float = DEFAULT_PENDING_GRACE_SECONDS

    def __post_init__(self) -> None:
        if self.max_mean_maker_fee_micros < 0:
            raise ValueError("max_mean_maker_fee_micros must be non-negative")
        if self.notable_maker_fee_micros < 0:
            raise ValueError("notable_maker_fee_micros must be non-negative")
        if self.good_mean_markout_cents <= self.min_mean_markout_cents:
            raise ValueError("good markout must be above the trip threshold")
        if self.min_fill_rate < 0:
            raise ValueError("min_fill_rate must be non-negative")
        if self.pending_grace_seconds <= 0:
            raise ValueError("pending_grace_seconds must be positive")


@dataclass
class CampaignMonitor:
    """Evaluates campaign premises. Halts on tripped or unmeasurable."""

    limits: CampaignLimits = field(default_factory=CampaignLimits)
    _halted: Reading | None = field(default=None, init=False, repr=False)

    @property
    def halted(self) -> bool:
        return self._halted is not None

    @property
    def halt_reason(self) -> Reading | None:
        return self._halted

    def assess(self, sample: CampaignSample) -> CampaignVerdict:
        """Evaluate every premise. A halt latches until cleared by a person."""

        readings = (
            self._maker_fee(sample),
            self._fee_reader_control(sample),
            self._markout(sample),
            self._fill_rate(sample),
            self._balance(sample),
            self._session_loss(sample),
        )
        verdict = CampaignVerdict(readings=readings)

        if verdict.should_halt and self._halted is None:
            self._halted = verdict.halting[0]

        return verdict

    def clear_halt(self) -> None:
        """Explicitly resume. Deliberately manual - see the module docstring."""

        self._halted = None

    def _pending_or_unknown(self, sample: CampaignSample, detail: str) -> Verdict:
        """PENDING while it is still early, UNKNOWN once it is not.

        The whole point of the grace period: "not enough data yet" is a fine
        answer for ten minutes and an alarming one after an hour of trading.
        """

        if sample.elapsed_seconds < self.limits.pending_grace_seconds:
            return Verdict.PENDING

        return Verdict.UNKNOWN

    def _maker_fee(self, sample: CampaignSample) -> Reading:
        makers = sample.maker_fills
        unreadable = [f for f in makers if f.fee_micros is None]
        readable = [f for f in makers if f.fee_micros is not None]

        if unreadable:
            # Never average around these. An unreadable fee is the failure mode
            # this monitor exists to catch.
            return Reading(
                "maker_fee",
                Verdict.UNKNOWN,
                f"{len(unreadable)} of {len(makers)} maker fill(s) reported no "
                "fee field - cannot confirm makers are still free",
            )

        if len(readable) < self.limits.min_maker_fills:
            return Reading(
                "maker_fee",
                self._pending_or_unknown(sample, "too few maker fills"),
                f"{len(readable)}/{self.limits.min_maker_fills} maker fills seen",
            )

        charged = sum(f.fee_micros for f in readable)
        mean = charged / len(readable)

        # A rebate. Exchanges pay makers to show up when they want depth, and
        # this is the shape that takes on the ledger.
        if charged < 0:
            return Reading(
                "maker_fee",
                Verdict.FAVOURABLE,
                f"makers were PAID ${-charged / MONEY_SCALE:.4f} across "
                f"{len(readable)} fill(s) - a rebate is running, and it will end",
            )

        if mean > self.limits.max_mean_maker_fee_micros:
            return Reading(
                "maker_fee",
                Verdict.TRIPPED,
                f"maker fills now average ${mean / MONEY_SCALE:.6f} "
                f"(${charged / MONEY_SCALE:.4f} over {len(readable)} fills) - the "
                "schedule changed and the thesis rests on makers being free",
            )

        if mean >= self.limits.notable_maker_fee_micros:
            # Below the economic bar but no longer zero. Worth surfacing: the
            # first sign of a schedule change is a small one.
            return Reading(
                "maker_fee",
                Verdict.OK,
                f"{len(readable)} maker fill(s), ${charged / MONEY_SCALE:.6f} charged "
                f"(mean ${mean / MONEY_SCALE:.6f}) - non-zero but below the "
                "economic bar; watch it",
            )

        return Reading(
            "maker_fee",
            Verdict.OK,
            f"{len(readable)} maker fill(s), $0.0000 charged",
        )

    def _fee_reader_control(self, sample: CampaignSample) -> Reading:
        """A zero maker fee is only evidence if takers are billed in the same run.

        Without this, a fee reader that returns zero for everything and a market
        that genuinely charges makers nothing produce identical output. That is
        not hypothetical: it is what happened, for a whole session.
        """

        takers = [f for f in sample.taker_fills if f.fee_micros is not None]
        charged = sum(f.fee_micros for f in takers)

        if not takers:
            # A strategy that never crosses generates no taker fills, so this
            # control can never satisfy itself from its own flow - it would
            # halt every pure-maker campaign after the grace period for a
            # reason the campaign cannot fix by trading better. The answer is a
            # deliberate probe, not a weaker rule: cross one contract
            # occasionally and read what it costs.
            return Reading(
                "fee_reader_control",
                self._pending_or_unknown(sample, "no taker fills"),
                "no taker fill to validate the fee reader against - a pure maker "
                "run must cross one contract deliberately (see "
                "scripts/queue_experiments.py --modes cross) or this can never "
                "pass",
            )

        if charged <= 0:
            return Reading(
                "fee_reader_control",
                Verdict.UNKNOWN,
                f"{len(takers)} taker fill(s) all read as free, which the fee "
                "formula forbids - the fee reader is broken, so no fee number "
                "here can be trusted",
            )

        return Reading(
            "fee_reader_control",
            Verdict.OK,
            f"{len(takers)} taker fill(s) charged ${charged / MONEY_SCALE:.4f}, "
            "so the reader works",
        )

    def _markout(self, sample: CampaignSample) -> Reading:
        scored = [
            f.markout_cents for f in sample.maker_fills if f.markout_cents is not None
        ]

        if len(scored) < self.limits.min_markout_fills:
            return Reading(
                "markout",
                self._pending_or_unknown(sample, "too few scored fills"),
                f"{len(scored)}/{self.limits.min_markout_fills} fills with a "
                "recorded mid at fill",
            )

        mean = sum(scored) / len(scored)

        if mean < self.limits.min_mean_markout_cents:
            return Reading(
                "markout",
                Verdict.TRIPPED,
                f"mean markout {mean:+.2f}c is below "
                f"{self.limits.min_mean_markout_cents:+.2f}c - resting quotes are "
                "being picked off",
            )

        if mean > self.limits.good_mean_markout_cents:
            return Reading(
                "markout",
                Verdict.FAVOURABLE,
                f"mean markout {mean:+.2f}c over {len(scored)} fills, well above "
                f"the {self.limits.good_mean_markout_cents:+.2f}c baseline - flow "
                "is unusually uninformed right now",
            )

        return Reading("markout", Verdict.OK, f"mean markout {mean:+.2f}c over {len(scored)} fills")

    def _fill_rate(self, sample: CampaignSample) -> Reading:
        if sample.quotes_placed <= 0:
            return Reading(
                "fill_rate",
                self._pending_or_unknown(sample, "no quotes placed"),
                "no quotes placed yet",
            )

        rate = len(sample.fills) / sample.quotes_placed

        if rate < self.limits.min_fill_rate:
            return Reading(
                "fill_rate",
                Verdict.TRIPPED,
                f"fill rate {rate:.1%} below {self.limits.min_fill_rate:.1%} - "
                "we are no longer reaching the front of the queue",
            )

        return Reading("fill_rate", Verdict.OK, f"fill rate {rate:.1%}")

    def _balance(self, sample: CampaignSample) -> Reading:
        floor = self.limits.min_balance_micros

        if floor is None:
            return Reading("balance", Verdict.OK, "no floor configured")

        if sample.balance_micros is None:
            return Reading(
                "balance",
                Verdict.UNKNOWN,
                "balance floor is configured but the balance could not be read",
            )

        if sample.balance_micros < floor:
            return Reading(
                "balance",
                Verdict.TRIPPED,
                f"balance ${sample.balance_micros / MONEY_SCALE:,.2f} below floor "
                f"${floor / MONEY_SCALE:,.2f}",
            )

        return Reading(
            "balance", Verdict.OK, f"balance ${sample.balance_micros / MONEY_SCALE:,.2f}"
        )

    def _session_loss(self, sample: CampaignSample) -> Reading:
        cap = self.limits.max_session_loss_micros

        if cap is None:
            return Reading("session_loss", Verdict.OK, "no cap configured")

        if sample.realized_pnl_micros is None:
            return Reading(
                "session_loss",
                Verdict.UNKNOWN,
                "a loss cap is configured but P&L could not be read",
            )

        if sample.realized_pnl_micros < -cap:
            return Reading(
                "session_loss",
                Verdict.TRIPPED,
                f"session P&L ${sample.realized_pnl_micros / MONEY_SCALE:,.2f} "
                f"past the ${cap / MONEY_SCALE:,.2f} cap",
            )

        return Reading(
            "session_loss",
            Verdict.OK,
            f"session P&L ${sample.realized_pnl_micros / MONEY_SCALE:,.2f}",
        )


def fills_from_ledger(raw_fills: Iterable[dict], *, mids: dict[str, int] | None = None) -> list[Fill]:
    """Build monitor fills from Kalshi's /portfolio/fills payload.

    Uses the strict fee reader, so a renamed field arrives here as None and
    trips the monitor rather than being silently counted as free.
    """

    from kalshi_mm_bot.api.parser import parse_fill_fee_micros
    from kalshi_mm_bot.market.price import parse_count_fp, parse_price_fp

    built: list[Fill] = []

    for raw in raw_fills:
        try:
            yes_price = parse_price_fp(str(raw.get("yes_price_dollars", "0")))
            count = parse_count_fp(str(raw.get("count_fp", "0")))
        except (TypeError, ValueError):
            continue

        built.append(
            Fill(
                yes_price=yes_price,
                count=count,
                is_taker=bool(raw.get("is_taker")),
                fee_micros=parse_fill_fee_micros(raw),
                mid_at_fill=(mids or {}).get(str(raw.get("fill_id") or "")),
                action=str(raw.get("action") or "buy"),
            )
        )

    return built
