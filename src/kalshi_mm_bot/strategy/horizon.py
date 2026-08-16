"""Market maker that prices adverse selection, inventory risk and fees explicitly.

This is `adaptive` with the three things that were costing money made into
first-class terms rather than fixed constants:

1. **Fees are checked against the actual quote, not approximated.** The
   adaptive strategy adds a per-contract fee edge, which is right as far as it
   goes, but Kalshi rounds each order's fee up to the cent. At the one- and
   two-contract sizes used in live testing that ceiling is a large fraction of
   the fee, so a quote that looks profitable per contract loses money per
   order. `_fee_viable_count` refuses to send an order whose captured edge
   cannot pay for itself at the size actually being sent.

2. **Adverse selection scales with measured volatility.** The edge a resting
   quote needs is not a constant, it is roughly `sigma * sqrt(how long the
   quote will rest)`. Estimating sigma from the book means the same parameters
   behave sensibly in a quiet market and in the last minute of a 15-minute
   crypto strike, without special-casing either.

3. **Time to close changes what inventory is worth, not just how fast things
   move.** Far from the close a position is a diffusion you can flatten out
   of. Near the close it is increasingly a coin flip you are stuck holding,
   worth `sqrt(P * (1 - P))` in risk terms. The strategy tapers size, widens
   quotes, then goes reduce-only, then flattens, driven by that blend.

`seconds_to_close` is optional throughout. With no close time the strategy
degrades to the volatility-aware behaviour and never invents a deadline.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field

from kalshi_mm_bot.market.dynamics import (
    MarketDynamicsTracker,
    MarketSnapshot,
    _forced_hold_probability,
)
from kalshi_mm_bot.market.orderbook import Orderbook
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR
from kalshi_mm_bot.market.types import MarketTicker, OrderAction
from kalshi_mm_bot.sim.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.strategy.params import ParamSpec
from kalshi_mm_bot.strategy.types import (
    PortfolioView,
    QuoteIntent,
    StrategyContext,
)

BPS_SCALE = 10_000

HORIZON_PARAM_SPEC = ParamSpec(
    count_params=frozenset({"count", "max_position", "min_count", "min_top_size"}),
    price_params=frozenset(
        {
            "min_profit_edge",
            "max_spread",
            "max_quote_away",
            "inventory_skew",
            "vol_reference_ticks",
            "edge_ramp_ticks",
            "max_fee_round_trip_ticks",
        }
    ),
    bps_params=frozenset(
        {
            "adverse_selection_bps",
            "inventory_risk_bps",
            "inventory_size_penalty_bps",
            "liquidity_fraction_bps",
        }
    ),
    seconds_params=frozenset(
        {
            "quote_lifetime_seconds",
            "flatten_seconds",
            "reduce_only_seconds",
            "stop_quoting_seconds",
        }
    ),
)
HORIZON_PARAMETER_NAMES = HORIZON_PARAM_SPEC.names


@dataclass(slots=True)
class HorizonAwareMarketMaker:
    """Volatility- and expiry-aware market maker with fee-viable sizing."""

    count: int = COUNT_SCALE
    max_position: int = 10 * COUNT_SCALE
    min_count: int = COUNT_SCALE

    # Edge components, all in price ticks unless named otherwise.
    min_profit_edge: int = 25
    adverse_selection_bps: int = 10_000
    quote_lifetime_seconds: float = 2.0

    # Inventory.
    inventory_skew: int = 300
    inventory_risk_bps: int = 5_000
    inventory_size_penalty_bps: int = 7_500
    flatten_seconds: float = 30.0

    # Book filters.
    max_spread: int = 1_000
    max_quote_away: int = 100
    liquidity_fraction_bps: int = 5_000
    min_top_size: int = COUNT_SCALE // 4
    use_microprice: bool = True
    quote_inside_spread: bool = True

    # Sizing.
    size_ramp_enabled: bool = True
    edge_ramp_ticks: int = 100
    vol_reference_ticks: int = 50

    # Expiry handling. None disables the phase.
    reduce_only_seconds: float | None = 120.0
    stop_quoting_seconds: float | None = 30.0
    flatten_before_close: bool = True

    # Fees.
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL
    require_fee_viable_quote: bool = True
    max_fee_round_trip_ticks: int | None = None

    name: str = "horizon"

    _tracker: MarketDynamicsTracker = field(
        default_factory=MarketDynamicsTracker,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.count <= 0:
            raise ValueError("count must be greater than zero")
        if self.max_position < 0:
            raise ValueError("max_position must be non-negative")
        if self.min_count <= 0:
            raise ValueError("min_count must be greater than zero")
        if self.quote_lifetime_seconds < 0:
            raise ValueError("quote_lifetime_seconds must be non-negative")
        if self.flatten_seconds <= 0:
            raise ValueError("flatten_seconds must be greater than zero")

    def on_orderbook(
        self,
        context: StrategyContext,
        market_ticker: MarketTicker,
        orderbook: Orderbook,
        portfolio: PortfolioView,
    ) -> tuple[QuoteIntent, ...]:
        snapshot = self._tracker.observe(
            market_ticker,
            orderbook,
            offset_seconds=context.offset_seconds,
            seconds_to_close=context.seconds_to_close,
        )

        if snapshot is None:
            return ()

        position = portfolio.position(market_ticker)
        seconds_to_close = context.seconds_to_close

        if self._past_stop_quoting(seconds_to_close):
            return self._flatten_intents(snapshot, position)

        if snapshot.spread > self.max_spread:
            return ()

        if not self._fee_band_allows(snapshot):
            return ()

        reduce_only = self._is_reduce_only(seconds_to_close)
        reservation = self._reservation_price(snapshot, position)
        required_edge = self._required_edge(snapshot)

        intents: list[QuoteIntent] = []

        for action in ("buy", "sell"):
            if reduce_only and _increases_exposure(action, position):
                continue

            intent = self._build_quote(
                snapshot,
                action=action,
                reservation=reservation,
                required_edge=required_edge,
                position=position,
            )

            if intent is not None:
                intents.append(intent)

        return tuple(intents)

    # ---- edge -----------------------------------------------------------

    def _required_edge(self, snapshot: MarketSnapshot) -> int:
        """Ticks of edge demanded on each side, before the per-order fee check."""

        fee_edge = self.fee_model.edge_ticks_per_contract(snapshot.mid)
        adverse = (
            self.adverse_selection_bps
            * snapshot.expected_move_ticks(self.quote_lifetime_seconds)
            / BPS_SCALE
        )
        return self.min_profit_edge + fee_edge + int(adverse)

    def _reservation_price(self, snapshot: MarketSnapshot, position: int) -> int:
        fair = snapshot.microprice if self.use_microprice else snapshot.mid

        if self.max_position <= 0:
            return _clamp(fair, 0, ONE_DOLLAR)

        normalized = _clamp(position, -self.max_position, self.max_position) / self.max_position
        risk_ticks = (
            self.inventory_risk_bps
            * snapshot.inventory_sigma_ticks(flatten_seconds=self.flatten_seconds)
            / BPS_SCALE
        )
        offset = normalized * (self.inventory_skew + risk_ticks)

        return _clamp(fair - int(offset), 0, ONE_DOLLAR)

    def _fee_band_allows(self, snapshot: MarketSnapshot) -> bool:
        """Reject prices where the round-trip fee exceeds the configured budget.

        Kalshi's fee peaks at $0.50 and vanishes in the tails, so this is the
        single most important control on whether market making can work at all.
        Off by default because the right threshold depends on the fee schedule
        the account is actually billed under - calibrate first, then set it.
        """

        if self.max_fee_round_trip_ticks is None:
            return True

        round_trip = self.fee_model.breakeven_edge_ticks(
            yes_price=snapshot.mid,
            count=self.count,
        )
        return round_trip <= self.max_fee_round_trip_ticks

    # ---- quoting --------------------------------------------------------

    def _build_quote(
        self,
        snapshot: MarketSnapshot,
        *,
        action: OrderAction,
        reservation: int,
        required_edge: int,
        position: int,
    ) -> QuoteIntent | None:
        price = self._quote_price(
            snapshot,
            action=action,
            reservation=reservation,
            required_edge=required_edge,
        )

        if price is None:
            return None

        captured_edge = reservation - price if action == "buy" else price - reservation

        if captured_edge <= 0:
            return None

        # How much edge the touch itself offers. Sizing keys off this rather
        # than off `captured_edge`, because once quotes sit at the required
        # edge by construction the captured figure is a constant and carries no
        # information about how good the opportunity is.
        available_edge = (
            reservation - snapshot.best_bid if action == "buy" else snapshot.best_ask - reservation
        )

        count = self._quote_count(
            snapshot,
            action=action,
            position=position,
            captured_edge=captured_edge,
            available_edge=available_edge,
            required_edge=required_edge,
            price=price,
        )

        if count <= 0:
            return None

        return QuoteIntent(
            quote_id=f"{snapshot.market_ticker}:horizon:yes:{action}",
            market_ticker=snapshot.market_ticker,
            action=action,
            side="yes",
            yes_price=price,
            count=count,
        )

    def _quote_price(
        self,
        snapshot: MarketSnapshot,
        *,
        action: OrderAction,
        reservation: int,
        required_edge: int,
    ) -> int | None:
        """Price the quote at reservation minus the edge we demand.

        The obvious implementation - `min(best_bid, reservation - edge)` - looks
        conservative and is how `adaptive` does it, but it silently disables
        every other control in the strategy. Whenever the book is wider than
        the required edge the quote pins to the touch, so inventory skew and
        volatility widening move a number that is then thrown away. In a 40/60
        book the strategy joins at 40 regardless of whether it is flat or at
        its position limit.

        Quoting at the target instead lets those controls do their job, and
        costs nothing in edge terms because `required_edge` is already the
        hurdle. `quote_inside_spread` restores the join-only behaviour for
        comparison runs.
        """

        levels = snapshot.price_levels

        if not levels:
            return None

        if action == "buy":
            target = reservation - required_edge

            if not self.quote_inside_spread:
                target = min(snapshot.best_bid, target)

            # One tick short of the ask: a resting buy at the ask would cross.
            target = min(target, snapshot.best_ask - 1)
            price = _floor_level(levels, target)
            too_far = price is None or snapshot.best_bid - price > self.max_quote_away
        else:
            target = reservation + required_edge

            if not self.quote_inside_spread:
                target = max(snapshot.best_ask, target)

            target = max(target, snapshot.best_bid + 1)
            price = _ceil_level(levels, target)
            too_far = price is None or price - snapshot.best_ask > self.max_quote_away

        if too_far or price is None:
            return None

        # Never rest a quote that would immediately trade against the book.
        if action == "buy" and price >= snapshot.best_ask:
            return None

        if action == "sell" and price <= snapshot.best_bid:
            return None

        return price

    def _quote_count(
        self,
        snapshot: MarketSnapshot,
        *,
        action: OrderAction,
        position: int,
        captured_edge: int,
        available_edge: int,
        required_edge: int,
        price: int,
    ) -> int:
        top_size = snapshot.bid_size if action == "buy" else snapshot.ask_size

        if top_size < self.min_top_size:
            return 0

        liquidity_count = top_size * self.liquidity_fraction_bps // BPS_SCALE
        ceiling = min(self.count, max(self.min_count, liquidity_count))
        desired = self._ramped_size(
            ceiling,
            available_edge=available_edge,
            required_edge=required_edge,
        )
        desired = int(desired * self._volatility_taper(snapshot))
        desired = int(desired * self._time_taper(snapshot))
        desired = _apply_inventory_size_penalty(
            desired,
            action=action,
            position=position,
            max_position=self.max_position,
            penalty_bps=self.inventory_size_penalty_bps,
        )

        capacity = (
            self.max_position - position if action == "buy" else self.max_position + position
        )
        count = min(desired, capacity, ceiling)

        if count < self.min_count:
            return 0

        return self._fee_viable_count(price=price, edge_ticks=captured_edge, count=count)

    def _ramped_size(self, ceiling: int, *, available_edge: int, required_edge: int) -> int:
        """Scale size with how far the market's own touch beats our hurdle.

        Quoting max size into a market that barely covers costs and max size
        into a market paying triple treats two very different opportunities
        identically. Ramping means the book only sees size when the edge is
        genuinely there, which also keeps average inventory lower for the same
        P&L. This was the untested lever - size was previously a constant.
        """

        if not self.size_ramp_enabled or self.edge_ramp_ticks <= 0:
            return ceiling

        surplus = available_edge - required_edge

        if surplus <= 0:
            return self.min_count

        ramp = min(1.0, surplus / self.edge_ramp_ticks)
        return self.min_count + int((ceiling - self.min_count) * ramp)

    def _volatility_taper(self, snapshot: MarketSnapshot) -> float:
        """Shrink size when the expected move over a quote's life grows."""

        if self.vol_reference_ticks <= 0:
            return 1.0

        move = snapshot.expected_move_ticks(self.quote_lifetime_seconds)
        return self.vol_reference_ticks / (self.vol_reference_ticks + move)

    def _time_taper(self, snapshot: MarketSnapshot) -> float:
        """Shrink size as being stuck with inventory becomes likely."""

        if snapshot.seconds_to_close is None:
            return 1.0

        return 1.0 - _forced_hold_probability(
            snapshot.seconds_to_close,
            self.flatten_seconds,
        )

    def _fee_viable_count(self, *, price: int, edge_ticks: int, count: int) -> int:
        """Zero out an order whose edge cannot cover its own fee.

        Checked against one execution, not a round trip: each side of the round
        trip pays for itself out of its own edge. Because the fee ceiling is
        per order, a size that is too small is strictly worse than not trading,
        so we return zero rather than quietly sending a losing order.
        """

        if not self.require_fee_viable_quote:
            return count

        viable = self.fee_model.minimum_viable_count(
            yes_price=price,
            edge_ticks=edge_ticks,
            max_count=count,
            round_trip=False,
        )

        return 0 if viable is None else count

    # ---- expiry phases --------------------------------------------------

    def _past_stop_quoting(self, seconds_to_close: float | None) -> bool:
        return (
            seconds_to_close is not None
            and self.stop_quoting_seconds is not None
            and seconds_to_close <= self.stop_quoting_seconds
        )

    def _is_reduce_only(self, seconds_to_close: float | None) -> bool:
        return (
            seconds_to_close is not None
            and self.reduce_only_seconds is not None
            and seconds_to_close <= self.reduce_only_seconds
        )

    def _flatten_intents(
        self,
        snapshot: MarketSnapshot,
        position: int,
    ) -> tuple[QuoteIntent, ...]:
        """Cross the spread to get flat in the final seconds.

        Deliberately marketable: holding a binary through resolution because we
        were unwilling to pay a taker fee is a far larger bet than the market
        making strategy ever intended to take.
        """

        if not self.flatten_before_close or position == 0:
            return ()

        action: OrderAction = "sell" if position > 0 else "buy"
        price = snapshot.best_bid if position > 0 else snapshot.best_ask

        return (
            QuoteIntent(
                quote_id=f"{snapshot.market_ticker}:horizon:yes:flatten",
                market_ticker=snapshot.market_ticker,
                action=action,
                side="yes",
                yes_price=price,
                count=abs(position),
            ),
        )


def parse_horizon_params(raw_values: str | list[str] | None) -> dict[str, int | float]:
    return HORIZON_PARAM_SPEC.parse(raw_values)


def format_horizon_params(params: Mapping[str, int | float]) -> str:
    return HORIZON_PARAM_SPEC.format(params)


def horizon_param_help() -> str:
    return (
        "Horizon overrides as key=value, comma separated or repeated. "
        "Count fields use contracts, price fields use decimals like 0.0200 or "
        "raw ticks, seconds fields use seconds. "
        f"Valid names: {', '.join(HORIZON_PARAMETER_NAMES)}."
    )


def _increases_exposure(action: OrderAction, position: int) -> bool:
    if position == 0:
        return True

    return (action == "buy") == (position > 0)


def _apply_inventory_size_penalty(
    count: int,
    *,
    action: OrderAction,
    position: int,
    max_position: int,
    penalty_bps: int,
) -> int:
    if max_position <= 0 or penalty_bps <= 0:
        return count

    risk_increasing_position = position if action == "buy" else -position

    if risk_increasing_position <= 0:
        return count

    penalty = min(BPS_SCALE, penalty_bps * risk_increasing_position // max_position)
    return count * (BPS_SCALE - penalty) // BPS_SCALE


def _floor_level(levels: tuple[int, ...], price: int) -> int | None:
    index = bisect_right(levels, price) - 1
    return levels[index] if index >= 0 else None


def _ceil_level(levels: tuple[int, ...], price: int) -> int | None:
    index = bisect_left(levels, price)
    return levels[index] if index < len(levels) else None


def _clamp(value: int, lower: int, upper: int) -> int:
    return min(max(value, lower), upper)
