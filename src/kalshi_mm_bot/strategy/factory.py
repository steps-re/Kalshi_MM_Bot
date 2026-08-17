from __future__ import annotations

from collections.abc import Mapping

from kalshi_mm_bot.strategy.adaptive import (
    AdaptivePredictionMarketMakerStrategy,
    parse_adaptive_params,
)
from kalshi_mm_bot.strategy.dumb import DumbMarketMakerStrategy
from kalshi_mm_bot.strategy.horizon import HorizonAwareMarketMaker, parse_horizon_params
from kalshi_mm_bot.strategy.types import Strategy

STRATEGY_NAMES: tuple[str, ...] = ("horizon", "adaptive", "dumb")


from kalshi_mm_bot.strategy.defended import MomentumDefendedStrategy
from kalshi_mm_bot.strategy.momentum_taker import MomentumTakerStrategy
from kalshi_mm_bot.strategy.phase import WindowPhaseStrategy


def strategy_from_name(
    name: str,
    *,
    count: int,
    max_position: int,
    adaptive_params: Mapping[str, int] | None = None,
) -> Strategy:
    normalized = name.strip().lower()

    if normalized in {"horizon", "horizon_aware", "expiry"}:
        params = dict(adaptive_params or ())
        params.setdefault("count", count)
        params.setdefault("max_position", max_position)
        return HorizonAwareMarketMaker(**params)

    if normalized in {"adaptive", "prediction", "adaptive_prediction_mm"}:
        params = dict(adaptive_params or ())
        params.setdefault("count", count)
        params.setdefault("max_position", max_position)
        return AdaptivePredictionMarketMakerStrategy(
            **params,
        )

    if normalized in {"momo", "momentum_taker", "taker"}:
        return MomentumTakerStrategy(count=count, max_position=max_position)

    # "defended:<name>" wraps any strategy in the momentum defence, so the two
    # can be compared by running the same inner strategy either way.
    if normalized.startswith("phased:"):
        return WindowPhaseStrategy(
            inner=strategy_from_name(
                normalized.split(":", 1)[1],
                count=count,
                max_position=max_position,
                adaptive_params=adaptive_params,
            )
        )

    if normalized.startswith(("defended:", "symmetric:")):
        prefix, inner_name = normalized.split(":", 1)
        return MomentumDefendedStrategy(
            inner=strategy_from_name(
                inner_name,
                count=count,
                max_position=max_position,
                adaptive_params=adaptive_params,
            ),
            symmetric=prefix == "symmetric",
        )

    if normalized in {"dumb", "benchmark", "dumb_join_top"}:
        return DumbMarketMakerStrategy(
            count=count,
            max_position=max_position,
        )

    raise ValueError(f"unknown strategy: {name!r}")


def parse_params_for(name: str, raw_values: str | list[str] | None) -> dict[str, int | float]:
    """Parse `key=value` overrides using the named strategy's own parameter set.

    `horizon` and `adaptive` accept different parameters, so parsing against
    the wrong one rejects valid overrides with a confusing "unknown parameter".
    """

    normalized = name.strip().lower()

    if normalized in {"horizon", "horizon_aware", "expiry"}:
        return parse_horizon_params(raw_values)

    return dict(parse_adaptive_params(raw_values))
