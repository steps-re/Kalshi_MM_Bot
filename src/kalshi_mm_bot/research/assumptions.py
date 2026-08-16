"""A ledger of the assumptions the opportunity model rests on.

The model that says "this is worth $X/day" is a product of four or five numbers
nobody has measured. Left as prose in a document, those numbers quietly become
facts: someone reads the conclusion, forgets the conditions, and six months
later the team is defending a figure whose foundations were never checked.

So the assumptions live here as data instead. Each one records what we assumed,
why it matters, how to measure it, and - once measured - whether reality agreed.
A measurement that refutes an assumption is the most valuable output this
project can produce, so the ledger is built to make refutation loud rather than
comfortable.

Three rules encoded here, learned the hard way elsewhere in this repo:

* **Unmeasured is not "probably fine".** An assumption with no measurement
  reports UNMEASURED, never PASS. Silence is not evidence.
* **A small sample is not a measurement.** Below `min_samples` the verdict is
  INSUFFICIENT, even if the observed number looks great. Four live sessions
  told us nothing, and it took a while to admit that.
* **Direction matters.** Being wrong in the direction that flatters the model
  is the dangerous one and is reported separately from being wrong in the
  direction that hurts it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    UNMEASURED = "unmeasured"
    INSUFFICIENT = "insufficient"
    CONFIRMED = "confirmed"
    OPTIMISTIC = "optimistic"  # reality is worse than we assumed
    CONSERVATIVE = "conservative"  # reality is better than we assumed

    @property
    def is_actionable(self) -> bool:
        """True when the model should be re-run before anyone quotes it again."""

        return self in {Verdict.OPTIMISTIC, Verdict.INSUFFICIENT, Verdict.UNMEASURED}


@dataclass(frozen=True, slots=True)
class Assumption:
    """One input the opportunity model depends on."""

    key: str
    statement: str
    assumed: float
    unit: str
    how_to_measure: str
    # Fractional tolerance before we call a difference material. 0.2 means we
    # accept reality landing within 20% of the assumption.
    tolerance: float = 0.20
    min_samples: int = 100
    # True when being wrong here invalidates the conclusion rather than
    # shading it. Blocking assumptions gate going live.
    blocking: bool = False

    def __post_init__(self) -> None:
        if self.tolerance < 0:
            raise ValueError("tolerance must be non-negative")
        if self.min_samples < 1:
            raise ValueError("min_samples must be at least one")


@dataclass(frozen=True, slots=True)
class Measurement:
    """What reality said, and how much of it we saw."""

    key: str
    observed: float
    sample_size: int
    measured_at_utc: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class Finding:
    assumption: Assumption
    measurement: Measurement | None
    verdict: Verdict

    @property
    def error(self) -> float | None:
        """Observed minus assumed, as a fraction of assumed."""

        if self.measurement is None or self.assumption.assumed == 0:
            return None

        return (self.measurement.observed - self.assumption.assumed) / abs(
            self.assumption.assumed
        )

    def describe(self) -> str:
        marker = "!!" if self.verdict is Verdict.OPTIMISTIC else "  "
        gate = " [BLOCKING]" if self.assumption.blocking else ""

        if self.measurement is None:
            return (
                f"{marker} {self.assumption.key:<24} {self.verdict.value.upper():<13}"
                f" assumed {self.assumption.assumed:g}{self.assumption.unit}{gate}\n"
                f"     -> {self.assumption.how_to_measure}"
            )

        error = self.error
        drift = f"{error:+.0%}" if error is not None else "n/a"
        return (
            f"{marker} {self.assumption.key:<24} {self.verdict.value.upper():<13}"
            f" assumed {self.assumption.assumed:g}{self.assumption.unit}, "
            f"observed {self.measurement.observed:g}{self.assumption.unit} "
            f"({drift}, n={self.measurement.sample_size}){gate}"
        )


@dataclass(slots=True)
class AssumptionLedger:
    """Assumptions, their measurements, and whether the model still stands."""

    assumptions: dict[str, Assumption] = field(default_factory=dict)
    measurements: dict[str, Measurement] = field(default_factory=dict)

    def register(self, assumption: Assumption) -> None:
        self.assumptions[assumption.key] = assumption

    def record(self, measurement: Measurement) -> None:
        if measurement.key not in self.assumptions:
            raise KeyError(
                f"no assumption registered for {measurement.key!r}; "
                "measure something we actually claimed"
            )

        self.measurements[measurement.key] = measurement

    def finding(self, key: str) -> Finding:
        assumption = self.assumptions[key]
        measurement = self.measurements.get(key)

        return Finding(
            assumption=assumption,
            measurement=measurement,
            verdict=_verdict(assumption, measurement),
        )

    def findings(self) -> tuple[Finding, ...]:
        return tuple(self.finding(key) for key in self.assumptions)

    @property
    def blocking_unresolved(self) -> tuple[Finding, ...]:
        """Blocking assumptions that are unmeasured, thin, or refuted.

        Non-empty means the model's headline number is not yet a claim anyone
        should act on.
        """

        return tuple(
            f
            for f in self.findings()
            if f.assumption.blocking and f.verdict.is_actionable
        )

    @property
    def ready_to_trade(self) -> bool:
        return not self.blocking_unresolved

    def describe(self) -> str:
        lines = ["assumption ledger:"]
        lines.extend(f.describe() for f in self.findings())

        blocking = self.blocking_unresolved

        if blocking:
            lines.append("")
            lines.append(
                f"NOT READY: {len(blocking)} blocking assumption(s) unresolved - "
                + ", ".join(f.assumption.key for f in blocking)
            )
        else:
            lines.append("")
            lines.append("All blocking assumptions measured and holding.")

        return "\n".join(lines)


def _verdict(assumption: Assumption, measurement: Measurement | None) -> Verdict:
    if measurement is None:
        return Verdict.UNMEASURED

    if measurement.sample_size < assumption.min_samples:
        return Verdict.INSUFFICIENT

    if assumption.assumed == 0:
        return Verdict.CONFIRMED if measurement.observed == 0 else Verdict.OPTIMISTIC

    drift = (measurement.observed - assumption.assumed) / abs(assumption.assumed)

    if abs(drift) <= assumption.tolerance:
        return Verdict.CONFIRMED

    # Every assumption here is oriented so that "lower than assumed" means the
    # model was flattering itself.
    return Verdict.OPTIMISTIC if drift < 0 else Verdict.CONSERVATIVE


def default_ledger(
    *,
    assumed_capture: float = 0.30,
    assumed_participation: float = 0.05,
    assumed_maker_fee_micros: float = 0.0,
    assumed_edge_cap_cents: float = 2.0,
    assumed_fill_rate: float = 0.10,
) -> AssumptionLedger:
    """The assumptions behind the published opportunity number.

    Defaults mirror the app's default sliders, so the ledger and the headline
    figure cannot drift apart without someone noticing.
    """

    ledger = AssumptionLedger()

    ledger.register(
        Assumption(
            key="maker_fee",
            statement="Resting (maker) fills are charged no fee on standard markets.",
            assumed=assumed_maker_fee_micros,
            unit=" micros/contract",
            how_to_measure=(
                "Run market/fees.py::calibrate_from_fills over real executions, "
                "comparing modelled fees against the fees_paid Kalshi reports."
            ),
            tolerance=0.0,
            min_samples=30,
            blocking=True,
        )
    )
    ledger.register(
        Assumption(
            key="spread_capture",
            statement="We keep 30% of the quoted spread after adverse selection.",
            assumed=assumed_capture,
            unit=" of spread",
            how_to_measure=(
                "analytics/markout.py over live or replayed fills: realised edge "
                "at a 30s horizon divided by the quoted edge at fill time."
            ),
            tolerance=0.25,
            min_samples=200,
            blocking=True,
        )
    )
    ledger.register(
        Assumption(
            key="participation",
            statement="We intermediate 5% of the volume in markets we quote.",
            assumed=assumed_participation,
            unit=" of volume",
            how_to_measure=(
                "Our filled contracts divided by the market's traded volume over "
                "the same window, per market."
            ),
            tolerance=0.40,
            min_samples=50,
            blocking=False,
        )
    )
    ledger.register(
        Assumption(
            key="edge_cap",
            statement="No round trip harvests more than 2c of edge.",
            assumed=assumed_edge_cap_cents,
            unit="c/round trip",
            how_to_measure=(
                "Distribution of realised edge per round trip. If the top decile "
                "clears the cap, wide markets are more harvestable than modelled."
            ),
            tolerance=0.50,
            min_samples=100,
            blocking=False,
        )
    )
    ledger.register(
        Assumption(
            key="fill_rate",
            statement="A resting quote fills often enough to recycle capital.",
            assumed=assumed_fill_rate,
            unit=" fills/quote",
            how_to_measure=(
                "Filled orders divided by placed orders. Drives how many times "
                "capital turns over per day, and therefore how much is needed."
            ),
            tolerance=0.50,
            min_samples=200,
            blocking=False,
        )
    )

    return ledger


def summarize(findings: Iterable[Finding]) -> str:
    collected: Sequence[Finding] = tuple(findings)

    if not collected:
        return "no assumptions registered"

    counts: dict[Verdict, int] = {}

    for finding in collected:
        counts[finding.verdict] = counts.get(finding.verdict, 0) + 1

    return ", ".join(
        f"{count} {verdict.value}" for verdict, count in sorted(counts.items())
    )
