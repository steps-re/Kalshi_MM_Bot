"""Time remaining until a market resolves.

Kept separate from the strategies because live and replay learn the close time
by completely different routes - the API in one case, the recording manifest in
the other - and because a wrong answer here is dangerous. A strategy that
believes a market closes sooner than it does will flatten into the spread for
no reason; one that believes it closes later will hold a binary through
resolution. Unknown is therefore represented as None everywhere and never
guessed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kalshi_mm_bot.market.types import MarketTicker


@dataclass(frozen=True, slots=True)
class MarketClock:
    """Maps tickers to close times and answers "how long left?"."""

    close_times_utc: dict[MarketTicker, datetime] = field(default_factory=dict)

    @classmethod
    def from_iso_map(cls, raw: Mapping[str, str] | None) -> "MarketClock":
        if not raw:
            return cls()

        parsed: dict[MarketTicker, datetime] = {}

        for ticker, raw_time in raw.items():
            close_time = parse_utc(raw_time)

            if close_time is not None:
                parsed[ticker] = close_time

        return cls(close_times_utc=parsed)

    def to_iso_map(self) -> dict[str, str]:
        return {
            ticker: close_time.isoformat().replace("+00:00", "Z")
            for ticker, close_time in self.close_times_utc.items()
        }

    def seconds_to_close(
        self,
        market_ticker: MarketTicker,
        *,
        now_utc: datetime | str | None = None,
    ) -> float | None:
        """Seconds until `market_ticker` closes, or None if unknown.

        Clamped at zero: a market past its close has no negative time left, and
        callers that branch on `<= threshold` would otherwise behave the same
        either way while reading confusingly in logs.
        """

        close_time = self.close_times_utc.get(market_ticker)

        if close_time is None:
            return None

        reference = _resolve_now(now_utc)

        if reference is None:
            return None

        return max(0.0, (close_time - reference).total_seconds())

    def knows(self, market_ticker: MarketTicker) -> bool:
        return market_ticker in self.close_times_utc


def parse_utc(raw_time: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating the trailing Z Kalshi sends."""

    if not raw_time:
        return None

    text = raw_time.strip()

    if text.endswith(("z", "Z")):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _resolve_now(now_utc: datetime | str | None) -> datetime | None:
    if now_utc is None:
        return datetime.now(UTC)

    if isinstance(now_utc, str):
        return parse_utc(now_utc)

    return now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=UTC)
