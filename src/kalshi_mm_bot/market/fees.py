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

Two things matter more than the headline rate:

* The ceiling is applied **per order**, not per contract. One contract at
  $0.50 owes $0.0175 and pays $0.02 - a 14% surcharge that only shows up at
  the small sizes we are actually testing with.
* Maker and taker schedules differ by market and have changed over time.
  Rather than hardcode a guess, `KalshiFeeModel` is configurable and
  `calibrate_from_fills` reconciles it against the `fees_paid` Kalshi reports
  on real executions. Run one live session, calibrate, then trust backtests.

Defaults are deliberately conservative: every fill is charged the taker
formula. That overstates cost if maker fills turn out to be cheaper, which is
the direction of error we want in a system that decides whether to risk money.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE, ONE_DOLLAR, PRICE_SCALE

BPS_SCALE = 10_000
CENT_MICROS = MONEY_SCALE // 100

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
        charge_makers_taker_rate: When True (default) every fill pays the
            formula regardless of maker/taker. Conservative.
        round_up_to_cent: Apply Kalshi's per-order ceiling. Leave True; turning
            it off is only useful for isolating how much the rounding costs.
    """

    trading_fee_bps: int = DEFAULT_TRADING_FEE_BPS
    maker_fee_per_contract_micros: int = DEFAULT_MAKER_FEE_PER_CONTRACT_MICROS
    charge_makers_taker_rate: bool = True
    round_up_to_cent: bool = True

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

        return _ceil_to_cent(raw) if self.round_up_to_cent else raw

    def edge_ticks_per_contract(self, yes_price: int) -> int:
        """Price edge, in ticks, needed to cover one side's fee on one contract.

        This is what a strategy should add to its required spread. It excludes
        the per-order ceiling, which cannot be expressed per contract - see
        `ceiling_surcharge_micros` for that piece.
        """

        if self.trading_fee_bps <= 0:
            return 0

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
    fills: Iterable[tuple[int, int, bool, int]],
) -> FeeCalibration:
    """Score `model` against real executions.

    Each fill is `(yes_price, count, is_taker, actual_fee_micros)`, which is
    exactly what `OrderFill` plus the account's reported `fees_paid` provide.
    A mismatch means the backtest is lying about costs - fix the model before
    trusting any optimizer output.
    """

    sample_count = 0
    modelled = 0
    actual = 0

    for yes_price, count, is_taker, actual_fee_micros in fills:
        sample_count += 1
        modelled += model.fee_micros(yes_price=yes_price, count=count, is_taker=is_taker)
        actual += actual_fee_micros

    return FeeCalibration(
        sample_count=sample_count,
        modelled_micros=modelled,
        actual_micros=actual,
    )


def _ceil_to_cent(micros: int) -> int:
    return _ceil_div(micros, CENT_MICROS) * CENT_MICROS


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _format_micros(micros: int) -> str:
    sign = "-" if micros < 0 else ""
    micros = abs(micros)
    return f"{sign}${micros // MONEY_SCALE}.{micros % MONEY_SCALE:06d}"
