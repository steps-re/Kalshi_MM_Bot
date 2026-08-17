"""Kalshi trading fees.

The simulator used to price fills at `price * count` and stop there, so every
backtest reported gross P&L while every live fill paid a fee. For a market
maker quoting a one or two tick edge that difference is not a rounding error,
it is the whole strategy: at $0.50 a single contract owes $0.0175, which is
comparable to the edge the quote was trying to capture in the first place.

Kalshi's published trading fee is

    fee = round_up_to_cent(rate * contracts * P * (1 - P))

with `rate` 0.07 on standard markets and P the price in dollars. Note that
P * (1 - P) is symmetric about $0.50, so YES and NO orders at equivalent
prices owe the same fee and we can always compute from the YES price.

**That published ceiling is wrong, and we measured it.** Kalshi's ledger rounds
up to $0.0001, not to the next cent. Across 48 taker fills the raw formula
predicts $0.5860 and Kalshi charged $0.5879; a cent ceiling would have charged
$0.8700. So `round_up_to_cent` now defaults to False.

The error only bites at small sizes, which is exactly why it survived: a ceiling
to a whole cent is nearly the entire fee when the fee is under two cents, and
almost invisible on an order of a few hundred contracts. Every live test we ran
used one contract.

Two things still matter more than the headline rate:

* Fees are charged **per order**, so a round trip pays twice. A strategy that
  budgets for one side is short by half.
* Maker and taker schedules differ. Measured on this account: **takers pay the
  formula and makers pay nothing** - 25 maker fills charged $0.0000 against 48
  taker fills charged $0.5879, across two independent series at mid prices,
  with the taker total serving as the control that proves the reader works.

`calibrate_from_fills` reconciles the model against what Kalshi actually billed.
Run one live session, calibrate, then trust backtests - and note that the two
corrections above were both found that way rather than by reading the docs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE, ONE_DOLLAR, PRICE_SCALE

BPS_SCALE = 10_000
CENT_MICROS = MONEY_SCALE // 100
# Measured, not published. Kalshi's documentation describes a per-order ceiling
# to the next cent; the ledger rounds up to a hundredth of a cent instead. Over
# 48 taker fills the raw formula predicts $0.5860 and Kalshi charged $0.5879,
# while ceiling to a whole cent would have charged $0.8700 - a 48% overstatement.
# Examples: 0.017472 -> 0.017500, 0.016492 -> 0.016500, 0.000966 -> 0.001000.
FEE_ROUNDING_MICROS = MONEY_SCALE // 10_000

DEFAULT_TRADING_FEE_BPS = 700
DEFAULT_MAKER_FEE_PER_CONTRACT_MICROS = 0


@dataclass(frozen=True, slots=True)
class KalshiFeeModel:
    """Fee schedule in money fixed point (1_000_000 == $1.00).

    Args:
        trading_fee_bps: Rate in the `rate * C * P * (1 - P)` formula. 700 bps
            is Kalshi's standard 0.07.
        maker_fee_per_contract_micros: Flat per-contract fee applied instead of
            the formula when a fill is known to be a maker fill and
            `charge_makers_taker_rate` is False.
        charge_makers_taker_rate: When True, every fill pays the taker formula
            regardless of who made the market. That was the default while the
            maker schedule was unknown, on the principle that overstating cost
            is the safe direction. It is now measured - 25 maker fills charged
            $0.0000 against 48 taker fills charged $0.5879, at midpoint prices
            in two independent series - so the default is False. Leaving it True
            does not make a backtest conservative any more, it makes it wrong:
            it charges roughly 1.75c per midpoint fill that the exchange does
            not charge, which is larger than the entire spread being captured
            and turns every profitable maker strategy into a losing one.
        round_up_to_cent: Round the per-order fee up to a whole cent. Kalshi's
            published description says it does this; its ledger does not, so
            this now defaults to False. It rounds up to $0.0001 instead. Leaving
            it True inflates the fee on small orders enormously - 48% across the
            one-contract fills we measured - because a ceiling to a cent is
            nearly the whole fee when the fee is under two cents. At realistic
            order sizes the two agree to well under a percent, which is why the
            error hid for so long: it only bites at the sizes used for testing.
    """

    trading_fee_bps: int = DEFAULT_TRADING_FEE_BPS
    maker_fee_per_contract_micros: int = DEFAULT_MAKER_FEE_PER_CONTRACT_MICROS
    charge_makers_taker_rate: bool = False
    round_up_to_cent: bool = False

    def __post_init__(self) -> None:
        if self.trading_fee_bps < 0:
            raise ValueError("trading_fee_bps must be non-negative")
        if self.maker_fee_per_contract_micros < 0:
            raise ValueError("maker_fee_per_contract_micros must be non-negative")

    def fee_micros(self, *, yes_price: int, count: int, is_taker: bool = True) -> int:
        """Fee owed on a single execution, in money fixed point."""

        if count <= 0:
            return 0

        if is_taker or self.charge_makers_taker_rate:
            raw = self._formula_micros(yes_price=yes_price, count=count)
        else:
            raw = self.maker_fee_per_contract_micros * count // COUNT_SCALE

        return _ceil_to_cent(raw) if self.round_up_to_cent else _ceil_to_fee_tick(raw)

    def edge_ticks_per_contract(self, yes_price: int, *, is_taker: bool = True) -> int:
        """Price edge, in ticks, needed to cover one side's fee on one contract.

        This is what a strategy should add to its required spread. It excludes
        the per-order ceiling, which cannot be expressed per contract - see
        `ceiling_surcharge_micros` for that piece.

        `is_taker` is not cosmetic. A **resting** quote pays no fee under the
        measured schedule, so demanding edge to cover one is demanding edge to
        cover nothing. That defaulted to the taker rate and quietly made the
        horizon strategy inert: at a midpoint price it required 1.75c of edge on
        every quote, which in a market quoted one cent wide put its bid two and a
        half cents below the touch, past max_quote_away, so it placed literally
        zero orders across 15,180 events. The strategy looked broken and the
        arithmetic was simply charging a maker a taker's fee.
        """

        if self.trading_fee_bps <= 0:
            return 0

        if not is_taker and not self.charge_makers_taker_rate:
            # Resting fills are free; a flat per-contract maker fee, if the
            # account has one, still has to be earned back.
            return _ceil_div(
                self.maker_fee_per_contract_micros * PRICE_SCALE, MONEY_SCALE
            )

        return _ceil_div(
            self.trading_fee_bps * yes_price * (ONE_DOLLAR - yes_price),
            ONE_DOLLAR * BPS_SCALE,
        )

    def ceiling_surcharge_micros(self, *, yes_price: int, count: int) -> int:
        """How much the per-order cent ceiling adds on top of the exact fee.

        Large at the one-contract sizes used for live testing, negligible once
        orders are a few hundred contracts. Worth reporting so nobody scales a
        strategy whose entire measured edge was rounding noise.
        """

        if count <= 0 or not self.round_up_to_cent:
            return 0

        raw = self._formula_micros(yes_price=yes_price, count=count)
        return _ceil_to_cent(raw) - raw

    def round_trip_micros(self, *, yes_price: int, count: int) -> int:
        """Fee to open and close `count` contracts at `yes_price`.

        Two separate executions, so the per-order ceiling applies twice. This
        is the number a quote has to beat, and it is the one that gets missed:
        a strategy that budgets for one side's fee is short by half.
        """

        entry = self.fee_micros(yes_price=yes_price, count=count, is_taker=False)
        exit_ = self.fee_micros(yes_price=yes_price, count=count, is_taker=True)
        return entry + exit_

    def minimum_viable_count(
        self,
        *,
        yes_price: int,
        edge_ticks: int,
        max_count: int,
        round_trip: bool = True,
    ) -> int | None:
        """Smallest order size whose captured edge covers its round-trip fee.

        The per-order ceiling does not scale with size, so it behaves like a
        fixed cost: one contract at $0.50 owes $0.0175 but pays $0.02 each way,
        meaning $0.04 of fees against an edge measured in fractions of a cent.
        There is therefore a minimum size below which a market maker cannot win
        no matter how good the quote is - and small live tests sit below it.

        Set `round_trip` False to check a single execution, which is what a
        one-sided quote should budget for: each side of a round trip covers its
        own fee out of its own edge, so charging both to one side would reject
        quotes that are in fact profitable.

        Returns None when no size up to `max_count` clears the bar, which is
        the signal to widen the quote rather than to trade bigger.
        """

        if edge_ticks <= 0 or max_count <= 0:
            return None

        # Edge scales linearly in count while the ceiling does not, so the
        # surplus is monotone once it turns positive: walk up by whole
        # contracts and take the first size that clears.
        count = COUNT_SCALE

        while count <= max_count:
            captured = edge_ticks * count
            required = (
                self.round_trip_micros(yes_price=yes_price, count=count)
                if round_trip
                else self.fee_micros(yes_price=yes_price, count=count, is_taker=False)
            )

            if captured >= required:
                return count

            count += COUNT_SCALE

        return None

    def breakeven_edge_ticks(self, *, yes_price: int, count: int) -> int:
        """Edge per contract, in ticks, needed to cover the round trip at `count`.

        Unlike `edge_ticks_per_contract` this includes the ceiling, so it rises
        sharply as size falls. Report it next to any small-size live result.
        """

        if count <= 0:
            return 0

        return _ceil_div(self.round_trip_micros(yes_price=yes_price, count=count), count)

    def _formula_micros(self, *, yes_price: int, count: int) -> int:
        # rate * contracts * P * (1 - P), carried in fixed point:
        #   contracts = count / COUNT_SCALE, P = yes_price / PRICE_SCALE
        # so the money-scaled result divides by BPS * COUNT_SCALE * PRICE_SCALE^2
        # and multiplies by MONEY_SCALE.
        numerator = (
            self.trading_fee_bps * count * yes_price * (ONE_DOLLAR - yes_price) * MONEY_SCALE
        )
        denominator = BPS_SCALE * COUNT_SCALE * PRICE_SCALE * PRICE_SCALE
        return numerator // denominator


ZERO_FEE_MODEL = KalshiFeeModel(trading_fee_bps=0, round_up_to_cent=False)
DEFAULT_FEE_MODEL = KalshiFeeModel()


@dataclass(frozen=True, slots=True)
class FeeCalibration:
    """Result of comparing a fee model against fees Kalshi actually charged."""

    sample_count: int
    modelled_micros: int
    actual_micros: int

    @property
    def error_micros(self) -> int:
        return self.modelled_micros - self.actual_micros

    @property
    def matches(self) -> bool:
        return self.sample_count > 0 and self.error_micros == 0

    def describe(self) -> str:
        if self.sample_count == 0:
            return "no fills to calibrate against"

        verdict = "matches" if self.matches else "MISMATCH"
        return (
            f"{verdict}: {self.sample_count} fill(s), "
            f"modelled {_format_micros(self.modelled_micros)}, "
            f"actual {_format_micros(self.actual_micros)}, "
            f"error {_format_micros(self.error_micros)}"
        )


def calibrate_from_fills(
    model: KalshiFeeModel,
    fills: Iterable[tuple[int, int, bool, int] | dict],
) -> FeeCalibration:
    """Score `model` against real executions.

    Each fill is `(yes_price, count, is_taker, actual_fee_micros)`, which is
    exactly what `OrderFill` plus the account's reported `fees_paid` provide.
    A mismatch means the backtest is lying about costs - fix the model before
    trusting any optimizer output.

    Mappings with those same keys are accepted too, because that is the shape
    `scripts/calibrate_fees.py` writes to disk. Requiring tuples here meant the
    one script that buys this measurement produced a file this function could
    not read, and the loop only closed by hand.
    """

    sample_count = 0
    modelled = 0
    actual = 0

    for yes_price, count, is_taker, actual_fee_micros in map(_as_fill_tuple, fills):
        sample_count += 1
        modelled += model.fee_micros(yes_price=yes_price, count=count, is_taker=is_taker)
        actual += actual_fee_micros

    return FeeCalibration(
        sample_count=sample_count,
        modelled_micros=modelled,
        actual_micros=actual,
    )


def _as_fill_tuple(fill: tuple[int, int, bool, int] | dict) -> tuple[int, int, bool, int]:
    if not isinstance(fill, dict):
        return fill

    return (
        int(fill["yes_price"]),
        int(fill["count"]),
        bool(fill["is_taker"]),
        int(fill["fee_micros"]),
    )


def _ceil_to_cent(micros: int) -> int:
    return _ceil_div(micros, CENT_MICROS) * CENT_MICROS


def _ceil_to_fee_tick(micros: int) -> int:
    """Round up to the granularity Kalshi's ledger actually bills at."""

    return _ceil_div(micros, FEE_ROUNDING_MICROS) * FEE_ROUNDING_MICROS


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _format_micros(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    micros = abs(micros)
    return f"{sign}${micros // MONEY_SCALE}.{micros % MONEY_SCALE:06d}"
