"""Time series of observed prices.

Lives under `market` rather than `analytics` because the simulator records one
while it runs, and analytics reads it afterwards. Putting it in either of those
packages would make them import each other.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass

from kalshi_mm_bot.market.types import MarketTicker


@dataclass(frozen=True, slots=True)
class MidSeries:
    """Mid price over time for one market, in fixed point, sorted by offset."""

    market_ticker: MarketTicker
    offsets: tuple[float, ...]
    mids: tuple[int, ...]

    def mid_at(self, offset_seconds: float) -> int | None:
        """Last observed mid at or before `offset_seconds`.

        Steps rather than interpolates: a price we never saw is not evidence.
        Returns None before the first observation, and the final mid after the
        last one - with a caveat the caller must respect, see `covers`.
        """

        if not self.offsets or offset_seconds < self.offsets[0]:
            return None

        index = bisect_left(self.offsets, offset_seconds)

        if index < len(self.offsets) and self.offsets[index] == offset_seconds:
            return self.mids[index]

        return self.mids[index - 1]

    def covers(self, offset_seconds: float) -> bool:
        """True when the series actually extends to `offset_seconds`.

        Markouts past the end of a recording are not zero, they are unknown.
        Counting them as zero would drag every horizon toward zero and make a
        badly adversely-selected strategy look merely mediocre.
        """

        return bool(self.offsets) and offset_seconds <= self.offsets[-1]
