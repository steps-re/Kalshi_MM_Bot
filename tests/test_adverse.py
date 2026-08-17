"""The adverse-selection fill model.

Its whole purpose is to stop the simulator handing us fills that reality would
not give us. The tests therefore check *which* fills survive, not how many.
"""

from kalshi_mm_bot.market.series import MidSeries
from kalshi_mm_bot.market.price import COUNT_SCALE, parse_price_fp
from kalshi_mm_bot.sim.adverse import AdverseSelectionFillModel
from kalshi_mm_bot.sim.fills import SimulatedFill
from kalshi_mm_bot.strategy.types import StrategyContext

ONE = COUNT_SCALE


def fill(action="buy", price="0.5000", ticker="M1"):
    return SimulatedFill(
        fill_id="f1",
        order_id="o1",
        market_ticker=ticker,
        action=action,
        side="yes",
        yes_price=parse_price_fp(price),
        count=ONE,
        offset_seconds=0.0,
        observed_at_utc=None,
        fill_model="queue",
        reason="same_level_reduction",
        is_taker=False,
    )


def series(*mids_at, ticker="M1"):
    offsets = tuple(float(o) for o, _ in mids_at)
    mids = tuple(int(m) for _, m in mids_at)
    return {ticker: MidSeries(market_ticker=ticker, offsets=offsets, mids=mids)}


class Inner:
    """A fill model that always produces the fills it is given."""

    name = "inner"

    def __init__(self, fills):
        self.fills = tuple(fills)

    def on_order_opened(self, order, book):
        pass

    def on_order_closed(self, order):
        pass

    def process_event(self, raw_msg, orderbooks, orders, context):
        return self.fills


def run(model, context_offset=0.0):
    return model.process_event(
        {}, {}, (), StrategyContext(event_count=1, offset_seconds=context_offset)
    )


def test_an_adverse_fill_is_always_kept() -> None:
    """Bought at 0.50, market fell to 0.48. Reality gives us this one."""

    model = AdverseSelectionFillModel(
        inner=Inner([fill(action="buy")]),
        mid_series=series((0.0, 5000), (30.0, 4800)),
        favourable_keep_rate=0.0,
    )

    assert len(run(model)) == 1
    assert model.kept_adverse == 1


def test_a_favourable_fill_can_be_dropped() -> None:
    """Bought at 0.50, market rose to 0.52 - the fill reality often withholds."""

    model = AdverseSelectionFillModel(
        inner=Inner([fill(action="buy")]),
        mid_series=series((0.0, 5000), (30.0, 5200)),
        favourable_keep_rate=0.0,
    )

    assert run(model) == ()
    assert model.dropped_favourable == 1


def test_direction_is_respected_for_sells() -> None:
    """A rising market is adverse to a seller and favourable to a buyer."""

    rising = series((0.0, 5000), (30.0, 5200))

    seller = AdverseSelectionFillModel(
        inner=Inner([fill(action="sell")]), mid_series=rising, favourable_keep_rate=0.0
    )
    buyer = AdverseSelectionFillModel(
        inner=Inner([fill(action="buy")]), mid_series=rising, favourable_keep_rate=0.0
    )

    assert len(run(seller)) == 1, "a rise is adverse to a short"
    assert run(buyer) == (), "and favourable to a long"


def test_keep_rate_of_one_is_the_inner_model() -> None:
    """The model must be switchable off, or it cannot be compared against."""

    model = AdverseSelectionFillModel(
        inner=Inner([fill()]),
        mid_series=series((0.0, 5000), (30.0, 5200)),
        favourable_keep_rate=1.0,
    )

    assert len(run(model)) == 1


def test_missing_forward_data_keeps_the_fill_and_counts_it_honestly() -> None:
    """At the end of a recording there is no future to judge against.

    Keeping the fill avoids inventing an outcome; counting it as favourable
    keeps the reported ratio from flattering the model.
    """

    model = AdverseSelectionFillModel(
        inner=Inner([fill()]),
        mid_series=series((0.0, 5000)),
        favourable_keep_rate=0.0,
        lookahead_seconds=30.0,
    )

    assert len(run(model)) == 1
    assert model.kept_favourable == 1
    assert model.dropped_favourable == 0


def test_thinning_is_deterministic() -> None:
    """Two runs of one recording must agree, or parameters cannot be compared."""

    def outcome():
        model = AdverseSelectionFillModel(
            inner=Inner([fill()]),
            mid_series=series((0.0, 5000), (30.0, 5200)),
            favourable_keep_rate=0.35,
        )
        return [len(run(model, context_offset=0.0)) for _ in range(20)]

    assert outcome() == outcome()


def test_an_unknown_market_does_not_crash_the_run() -> None:
    model = AdverseSelectionFillModel(
        inner=Inner([fill(ticker="OTHER")]),
        mid_series=series((0.0, 5000), (30.0, 5200)),
        favourable_keep_rate=0.0,
    )

    assert len(run(model)) == 1
