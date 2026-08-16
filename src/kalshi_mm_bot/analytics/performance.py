"""Session performance reporting.

Two numbers decide whether a market-making session was good, and neither is
total P&L. The first is **P&L attribution**: how much came from capturing
spread versus from being long or short while the market moved, minus fees. A
session that made money because the market happened to drift in favour of an
accidental position is not a working market maker, it is a directional bet with
extra steps. The second is **risk-adjusted return**, because a strategy that
makes a dollar with a five dollar drawdown cannot be scaled.

Everything here works off a fill list and an equity curve, so the same report
runs against a replay and against a live session.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import sqrt
from statistics import fmean, pstdev

from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE
from kalshi_mm_bot.sim.fees import DEFAULT_FEE_MODEL, KalshiFeeModel
from kalshi_mm_bot.sim.fills import SimulatedFill

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


@dataclass(frozen=True, slots=True)
class PnlAttribution:
    """Where the money came from, in money fixed point."""

    spread_capture: int
    inventory_pnl: int
    fees_paid: int

    @property
    def gross(self) -> int:
        return self.spread_capture + self.inventory_pnl

    @property
    def net(self) -> int:
        return self.gross - self.fees_paid

    @property
    def fee_share_of_gross(self) -> float | None:
        """Fees as a fraction of gross P&L. Above 1.0 means fees ate everything."""

        if self.gross <= 0:
            return None

        return self.fees_paid / self.gross


@dataclass(frozen=True, slots=True)
class RiskMetrics:
    sample_count: int
    sharpe: float | None
    max_drawdown: int
    peak_equity: int
    final_equity: int
    time_in_drawdown: float


@dataclass(frozen=True, slots=True)
class FillMetrics:
    fill_count: int
    maker_count: int
    taker_count: int
    buy_contracts: int
    sell_contracts: int
    mean_fill_size: float
    turnover_contracts: float

    @property
    def maker_share(self) -> float:
        return self.maker_count / self.fill_count if self.fill_count else 0.0

    @property
    def imbalance_contracts(self) -> float:
        return (self.buy_contracts - self.sell_contracts) / COUNT_SCALE


@dataclass(frozen=True, slots=True)
class PerformanceReport:
    attribution: PnlAttribution
    risk: RiskMetrics
    fills: FillMetrics
    fee_ceiling_surcharge: int
    breakeven_edge_ticks: float

    def describe(self) -> str:
        lines = [
            "P&L attribution:",
            f"  spread capture   {_money(self.attribution.spread_capture)}",
            f"  inventory P&L    {_money(self.attribution.inventory_pnl)}",
            f"  fees            -{_money(self.attribution.fees_paid)}",
            f"  net              {_money(self.attribution.net)}",
        ]

        share = self.attribution.fee_share_of_gross

        if share is not None:
            verdict = " - fees exceed gross P&L" if share > 1.0 else ""
            lines.append(f"  fees / gross     {share:.1%}{verdict}")

        if self.fee_ceiling_surcharge:
            lines.append(
                f"  of which per-order rounding: {_money(self.fee_ceiling_surcharge)} "
                "(shrinks with larger orders)"
            )

        lines.extend(
            [
                "",
                "Risk:",
                f"  max drawdown     {_money(self.risk.max_drawdown)}",
                f"  peak equity      {_money(self.risk.peak_equity)}",
                f"  final equity     {_money(self.risk.final_equity)}",
                f"  time in drawdown {self.risk.time_in_drawdown:.1%}",
            ]
        )

        if self.risk.sharpe is not None:
            lines.append(f"  sharpe (annualised) {self.risk.sharpe:.2f}")
        else:
            lines.append("  sharpe           not enough samples")

        lines.extend(
            [
                "",
                "Execution:",
                f"  fills            {self.fills.fill_count} "
                f"({self.fills.maker_share:.0%} maker)",
                f"  contracts        {self.fills.turnover_contracts:.2f}",
                f"  mean fill size   {self.fills.mean_fill_size:.2f} contracts",
                f"  buy/sell skew    {self.fills.imbalance_contracts:+.2f} contracts",
                f"  breakeven edge   {self.breakeven_edge_ticks:.1f} ticks at mean fill size",
            ]
        )

        return "\n".join(lines)


def build_performance_report(
    fills: Sequence[SimulatedFill],
    equity_curve: Sequence[tuple[float, int]],
    *,
    fees_paid: int,
    final_position: int | None = None,
    final_mid: int | None = None,
    final_marks: Mapping[str, tuple[int, int | None]] | None = None,
    fee_model: KalshiFeeModel = DEFAULT_FEE_MODEL,
) -> PerformanceReport:
    """Assemble the full report from a session's fills and equity curve."""

    return PerformanceReport(
        attribution=attribute_pnl(
            fills,
            fees_paid=fees_paid,
            final_position=final_position,
            final_mid=final_mid,
            final_marks=final_marks,
        ),
        risk=risk_metrics(equity_curve),
        fills=fill_metrics(fills),
        fee_ceiling_surcharge=sum(
            fee_model.ceiling_surcharge_micros(yes_price=fill.yes_price, count=fill.count)
            for fill in fills
        ),
        breakeven_edge_ticks=_breakeven_at_mean_size(fills, fee_model),
    )


def attribute_pnl(
    fills: Sequence[SimulatedFill],
    *,
    fees_paid: int,
    final_position: int | None = None,
    final_mid: int | None = None,
    final_marks: Mapping[str, tuple[int, int | None]] | None = None,
) -> PnlAttribution:
    """Split P&L into spread capture and inventory drift.

    Each fill is scored against the mid at the moment it happened: buying below
    mid or selling above it is spread capture, and it is the only part a market
    maker controls. Whatever the resulting inventory then does as the mid moves
    is inventory P&L - real money, but not evidence the strategy works.

    Without a mid at fill time we fall back to attributing everything to
    inventory, which understates the strategy rather than flattering it.

    Pass `final_marks` as `{ticker: (position, mid)}` for a session spanning
    several markets. A single `final_position`/`final_mid` pair only values
    residual inventory correctly when every fill is in one market, so it is
    rejected when the fills say otherwise rather than quietly marking one
    market's position at another market's price.
    """

    marks = _resolve_marks(fills, final_position, final_mid, final_marks)

    spread_capture = 0
    signed_cash = 0

    for fill in fills:
        direction = 1 if fill.action == "buy" else -1
        signed_cash -= direction * fill.yes_price * fill.count

        if fill.mid_at_fill is None:
            continue

        # Edge relative to fair value at the time, in ticks * contracts.
        spread_capture += direction * (fill.mid_at_fill - fill.yes_price) * fill.count

    closing_value = sum(
        position * mid for position, mid in marks.values() if mid is not None
    )
    gross = signed_cash + closing_value

    return PnlAttribution(
        spread_capture=spread_capture,
        inventory_pnl=gross - spread_capture,
        fees_paid=fees_paid,
    )


def _resolve_marks(
    fills: Sequence[SimulatedFill],
    final_position: int | None,
    final_mid: int | None,
    final_marks: Mapping[str, tuple[int, int | None]] | None,
) -> dict[str, tuple[int, int | None]]:
    if final_marks is not None:
        return dict(final_marks)

    tickers = {fill.market_ticker for fill in fills}

    if len(tickers) > 1:
        raise ValueError(
            "fills span multiple markets; pass final_marks={ticker: (position, mid)} "
            f"rather than a single position (saw {sorted(tickers)})"
        )

    ticker = next(iter(tickers), "")
    return {ticker: (final_position or 0, final_mid)}


def risk_metrics(equity_curve: Sequence[tuple[float, int]]) -> RiskMetrics:
    """Drawdown and Sharpe from an equity curve of (offset_seconds, value)."""

    if not equity_curve:
        return RiskMetrics(
            sample_count=0,
            sharpe=None,
            max_drawdown=0,
            peak_equity=0,
            final_equity=0,
            time_in_drawdown=0.0,
        )

    peak = equity_curve[0][1]
    max_drawdown = 0
    underwater_seconds = 0.0

    for index, (offset, value) in enumerate(equity_curve):
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, peak - value)

        if value < peak:
            # Weight by elapsed time, not sample count: equity samples are not
            # evenly spaced, and a burst of updates during one busy minute
            # would otherwise outvote an hour of quiet drawdown.
            next_offset = (
                equity_curve[index + 1][0] if index + 1 < len(equity_curve) else offset
            )
            underwater_seconds += max(0.0, next_offset - offset)

    elapsed = equity_curve[-1][0] - equity_curve[0][0]

    return RiskMetrics(
        sample_count=len(equity_curve),
        sharpe=_annualised_sharpe(equity_curve),
        max_drawdown=max_drawdown,
        peak_equity=peak,
        final_equity=equity_curve[-1][1],
        time_in_drawdown=(underwater_seconds / elapsed) if elapsed > 0 else 0.0,
    )


def fill_metrics(fills: Sequence[SimulatedFill]) -> FillMetrics:
    buy_contracts = sum(fill.count for fill in fills if fill.action == "buy")
    sell_contracts = sum(fill.count for fill in fills if fill.action == "sell")
    total = buy_contracts + sell_contracts

    return FillMetrics(
        fill_count=len(fills),
        maker_count=sum(1 for fill in fills if not fill.is_taker),
        taker_count=sum(1 for fill in fills if fill.is_taker),
        buy_contracts=buy_contracts,
        sell_contracts=sell_contracts,
        mean_fill_size=(total / len(fills) / COUNT_SCALE) if fills else 0.0,
        turnover_contracts=total / COUNT_SCALE,
    )


def _annualised_sharpe(equity_curve: Sequence[tuple[float, int]]) -> float | None:
    """Sharpe of the equity increments, scaled to a year.

    Uses absolute changes rather than percentage returns because a market maker
    running on posted collateral has no meaningful denominator, and because
    equity can legitimately pass through zero. Needs a real sample: on a ten
    minute session this is indicative at best, which is why the caller should
    print the sample count next to it.
    """

    if len(equity_curve) < 3:
        return None

    changes = [
        equity_curve[index][1] - equity_curve[index - 1][1]
        for index in range(1, len(equity_curve))
    ]
    deviation = pstdev(changes)

    if deviation == 0:
        return None

    elapsed = equity_curve[-1][0] - equity_curve[0][0]

    if elapsed <= 0:
        return None

    periods_per_year = SECONDS_PER_YEAR / (elapsed / len(changes))

    return fmean(changes) / deviation * sqrt(periods_per_year)


def _breakeven_at_mean_size(
    fills: Sequence[SimulatedFill],
    fee_model: KalshiFeeModel,
) -> float:
    if not fills:
        return 0.0

    mean_count = max(1, sum(fill.count for fill in fills) // len(fills))
    mean_price = sum(fill.yes_price for fill in fills) // len(fills)

    return float(fee_model.breakeven_edge_ticks(yes_price=mean_price, count=mean_count))


def _money(value: int) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}${value // MONEY_SCALE}.{value % MONEY_SCALE:06d}"


def summarize(reports: Iterable[PerformanceReport]) -> str:
    collected = list(reports)

    if not collected:
        return "no sessions to summarize"

    net = sum(report.attribution.net for report in collected)
    fees = sum(report.attribution.fees_paid for report in collected)
    spread = sum(report.attribution.spread_capture for report in collected)

    return (
        f"{len(collected)} session(s): net {_money(net)}, "
        f"spread capture {_money(spread)}, fees {_money(fees)}"
    )
