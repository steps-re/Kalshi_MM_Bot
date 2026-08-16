"""Out-of-sample validation for optimizer output.

The optimizer picks the best parameters on the recording it was given. On a
single 10-minute recording and a grid of a few hundred combinations, the best
result is mostly a description of that recording's noise - the "best" numbers
would have been just as impressive if the P&L column had been shuffled.

Walk-forward is the cheap fix: fit on earlier recordings, score on a later one
the fit never saw, and report both. The gap between them is the honest estimate
of how much of the in-sample result was real. A strategy whose out-of-sample
score is near zero or negative has not been optimized, it has been overfit, and
no amount of live testing at small size will reveal that faster than this will.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean

from kalshi_mm_bot.sim.backtest import BacktestSummary, run_replay_backtest
from kalshi_mm_bot.sim.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.sim.fills import FillModel
from kalshi_mm_bot.sim.optimization import (
    DEFAULT_OPTIMIZATION_OBJECTIVE,
    OptimizationObjective,
    _objective_value,
    strategy_parameter_grid,
)
from kalshi_mm_bot.strategy import strategy_from_name
from kalshi_mm_bot.strategy.requote import RequotePolicy

ProgressCallback = Callable[[str], None]
StopRequested = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class RecordingScore:
    recording: Path
    objective_value: int
    summary: BacktestSummary


@dataclass(frozen=True, slots=True)
class ParameterScore:
    """Aggregate performance of one parameter set over a set of recordings."""

    params: dict[str, int]
    scores: tuple[RecordingScore, ...]

    @property
    def total(self) -> int:
        return sum(score.objective_value for score in self.scores)

    @property
    def mean(self) -> float:
        return fmean(score.objective_value for score in self.scores) if self.scores else 0.0

    @property
    def worst(self) -> int:
        return min((score.objective_value for score in self.scores), default=0)

    @property
    def profitable_fraction(self) -> float:
        if not self.scores:
            return 0.0

        wins = sum(1 for score in self.scores if score.objective_value > 0)
        return wins / len(self.scores)


@dataclass(frozen=True, slots=True)
class WalkForwardFold:
    train: tuple[Path, ...]
    test: tuple[Path, ...]
    chosen: ParameterScore
    tested: ParameterScore

    @property
    def in_sample(self) -> int:
        return self.chosen.total

    @property
    def out_of_sample(self) -> int:
        return self.tested.total


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    folds: tuple[WalkForwardFold, ...]
    objective: OptimizationObjective

    @property
    def total_out_of_sample(self) -> int:
        return sum(fold.out_of_sample for fold in self.folds)

    @property
    def total_in_sample(self) -> int:
        return sum(fold.in_sample for fold in self.folds)

    @property
    def overfit_ratio(self) -> float | None:
        """Out-of-sample divided by in-sample. Near 1.0 is good, near 0 is noise.

        None when the in-sample total is non-positive, because the ratio stops
        meaning anything once the numerator and denominator can differ in sign.
        """

        if self.total_in_sample <= 0:
            return None

        return self.total_out_of_sample / self.total_in_sample

    def describe(self) -> str:
        lines = [
            f"walk-forward over {len(self.folds)} fold(s), objective={self.objective}",
            f"  in-sample total:     {self.total_in_sample}",
            f"  out-of-sample total: {self.total_out_of_sample}",
        ]

        ratio = self.overfit_ratio

        if ratio is not None:
            lines.append(f"  retained out of sample: {ratio:.1%}")

        for index, fold in enumerate(self.folds, start=1):
            lines.append(
                f"  fold {index}: train={len(fold.train)} test={len(fold.test)} "
                f"in={fold.in_sample} out={fold.out_of_sample} "
                f"params={fold.chosen.params}"
            )

        return "\n".join(lines)


async def score_params(
    recordings: Sequence[str | Path],
    *,
    params: Mapping[str, int],
    count: int,
    max_position: int,
    fill_model_factory: Callable[[], FillModel],
    strategy_name: str = "horizon",
    objective: OptimizationObjective = DEFAULT_OPTIMIZATION_OBJECTIVE,
    latency_seconds: float = 0.0,
    requote_policy: RequotePolicy | None = None,
    starting_balance_cents: int | None = None,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
) -> ParameterScore:
    """Replay one parameter set across every recording and aggregate."""

    trial_params = dict(params)
    trial_params.setdefault("count", count)
    trial_params.setdefault("max_position", max_position)
    scores: list[RecordingScore] = []

    for recording in recordings:
        strategy = strategy_from_name(
            strategy_name,
            count=count,
            max_position=max_position,
            adaptive_params=trial_params,
        )
        result = await run_replay_backtest(
            recording,
            strategy=strategy,
            fill_model=fill_model_factory(),
            latency_seconds=latency_seconds,
            requote_policy=requote_policy,
            starting_balance_cents=starting_balance_cents,
            fee_model=fee_model,
        )
        scores.append(
            RecordingScore(
                recording=Path(recording),
                objective_value=_objective_value(result.summary, objective),
                summary=result.summary,
            )
        )

    return ParameterScore(params=trial_params, scores=tuple(scores))


async def walk_forward(
    recordings: Sequence[str | Path],
    *,
    count: int,
    max_position: int,
    fill_model_factory: Callable[[], FillModel],
    search_space: Mapping[str, Sequence[int]] | None = None,
    fixed_params: Mapping[str, int] | None = None,
    strategy_name: str = "horizon",
    objective: OptimizationObjective = DEFAULT_OPTIMIZATION_OBJECTIVE,
    min_train_recordings: int = 2,
    latency_seconds: float = 0.0,
    requote_policy: RequotePolicy | None = None,
    starting_balance_cents: int | None = None,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
    selection: str = "worst",
    on_progress: ProgressCallback | None = None,
    stop_requested: StopRequested | None = None,
) -> WalkForwardResult:
    """Expanding-window walk-forward over recordings in chronological order.

    Fold k fits on recordings[:k] and scores on recordings[k], so every
    out-of-sample number comes from data the fit had not seen.

    `selection` decides which parameter set wins in-sample. The default is
    "worst" - maximise the worst recording rather than the total - because a
    parameter set that made everything back on one lucky replay is exactly what
    we are trying not to ship. Use "total" for the conventional choice.
    """

    ordered = [Path(recording) for recording in recordings]

    if len(ordered) < min_train_recordings + 1:
        raise ValueError(
            f"walk-forward needs at least {min_train_recordings + 1} recordings, "
            f"got {len(ordered)}"
        )

    grid = strategy_parameter_grid(search_space, fixed_params)
    folds: list[WalkForwardFold] = []

    for split in range(min_train_recordings, len(ordered)):
        if stop_requested is not None and stop_requested():
            break

        train = tuple(ordered[:split])
        test = (ordered[split],)
        best: ParameterScore | None = None

        for params in grid:
            if stop_requested is not None and stop_requested():
                break

            candidate = await score_params(
                train,
                params=params,
                count=count,
                max_position=max_position,
                fill_model_factory=fill_model_factory,
                strategy_name=strategy_name,
                objective=objective,
                latency_seconds=latency_seconds,
                requote_policy=requote_policy,
                starting_balance_cents=starting_balance_cents,
                fee_model=fee_model,
            )

            if best is None or _selection_key(candidate, selection) > _selection_key(
                best,
                selection,
            ):
                best = candidate

        if best is None:
            continue

        tested = await score_params(
            test,
            params=best.params,
            count=count,
            max_position=max_position,
            fill_model_factory=fill_model_factory,
            strategy_name=strategy_name,
            objective=objective,
            latency_seconds=latency_seconds,
            requote_policy=requote_policy,
            starting_balance_cents=starting_balance_cents,
            fee_model=fee_model,
        )
        folds.append(
            WalkForwardFold(train=train, test=test, chosen=best, tested=tested)
        )

        if on_progress is not None:
            on_progress(
                f"fold {len(folds)}: in={best.total} out={tested.total} params={best.params}"
            )

    return WalkForwardResult(folds=tuple(folds), objective=objective)


def _selection_key(score: ParameterScore, selection: str) -> tuple[float, float]:
    if selection == "total":
        return float(score.total), float(score.worst)

    if selection == "worst":
        return float(score.worst), float(score.total)

    raise ValueError(f"unknown selection rule: {selection!r}")
