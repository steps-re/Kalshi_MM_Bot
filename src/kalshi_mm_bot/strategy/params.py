"""Generic `key=value` parsing for strategy overrides.

`adaptive.py` grew its own copy of this. Rather than fork it again for every
new strategy, a strategy declares which bucket each parameter falls into and
gets parsing, validation and formatting from here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from kalshi_mm_bot.market.price import (
    format_count_fp,
    format_price_fp,
    parse_count_fp,
    parse_price_fp,
)


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Which parsing rule applies to each named parameter of a strategy."""

    count_params: frozenset[str] = frozenset()
    price_params: frozenset[str] = frozenset()
    bps_params: frozenset[str] = frozenset()
    int_params: frozenset[str] = frozenset()
    seconds_params: frozenset[str] = frozenset()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                self.count_params
                | self.price_params
                | self.bps_params
                | self.int_params
                | self.seconds_params
            )
        )

    def parse(self, raw_values: str | Iterable[str] | None) -> dict[str, int | float]:
        params: dict[str, int | float] = {}

        for entry in _entries(raw_values):
            name, separator, raw_value = entry.partition("=")

            if not separator:
                raise ValueError(f"invalid parameter {entry!r}; expected key=value")

            name = name.strip()

            if name not in self.names:
                raise ValueError(
                    f"unknown parameter {name!r}; valid names: {', '.join(self.names)}"
                )

            params[name] = self._parse_value(name, raw_value.strip())

        return params

    def format(self, params: Mapping[str, int | float]) -> str:
        return ", ".join(
            f"{name}={self._format_value(name, value)}"
            for name, value in sorted(params.items())
        )

    def _parse_value(self, name: str, raw_value: str) -> int | float:
        if not raw_value:
            raise ValueError(f"empty value for parameter {name!r}")

        if name in self.count_params:
            return parse_count_fp(raw_value)

        if name in self.price_params:
            return parse_price_fp(raw_value) if "." in raw_value else int(raw_value)

        if name in self.seconds_params:
            value = float(raw_value)

            if value < 0:
                raise ValueError(f"{name} must be non-negative")

            return value

        value = int(raw_value)

        if name in self.bps_params and value < 0:
            raise ValueError(f"{name} must be non-negative")

        return value

    def _format_value(self, name: str, value: int | float) -> str:
        if name in self.count_params:
            return format_count_fp(int(value))

        if name in self.price_params:
            return format_price_fp(int(value))

        if name in self.seconds_params:
            return f"{float(value):g}"

        return str(int(value))


def _entries(raw_values: str | Iterable[str] | None) -> tuple[str, ...]:
    if raw_values is None:
        return ()

    values = (raw_values,) if isinstance(raw_values, str) else raw_values
    entries: list[str] = []

    for raw_text in values:
        for entry in raw_text.replace(";", ",").split(","):
            entry = entry.strip()

            if entry:
                entries.append(entry)

    return tuple(entries)
