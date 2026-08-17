from datetime import UTC, datetime

from kalshi_mm_bot.analytics.markout import (
    MidSeries,
    compute_markout,
    markout_by_time_to_close,
)
from kalshi_mm_bot.analytics.performance import (
    attribute_pnl,
    fill_metrics,
    risk_metrics,
)
from kalshi_mm_bot.analytics.screening import (
    MarketQuote,
    by_price_band,
    capturable_ticks,
    distance_from_end,
    price_band,
    parse_market,
    parse_markets,
    screen_markets,
    score_market,
    tick_at_price,
    viable_price_band,
)
from kalshi_mm_bot.market.price import COUNT_SCALE
from kalshi_mm_bot.sim.fills import SimulatedFill

ONE = COUNT_SCALE


def make_fill(
    *,
    action="buy",
    price=5000,
    count=ONE,
    offset=0.0,
    mid=None,
    seconds_to_close=None,
    is_taker=False,
) -> SimulatedFill:
    return SimulatedFill(
        fill_id=f"f{offset}",
        order_id="o1",
        market_ticker="M1",
        action=action,
        side="yes",
        yes_price=price,
        count=count,
        offset_seconds=offset,
        observed_at_utc=None,
        fill_model="queue",
        reason="same_level_reduction",
        is_taker=is_taker,
        seconds_to_close=seconds_to_close,
        mid_at_fill=mid,
    )


def series(points) -> dict[str, MidSeries]:
    return {
        "M1": MidSeries(
            market_ticker="M1",
            offsets=tuple(offset for offset, _ in points),
            mids=tuple(mid for _, mid in points),
        )
    }


# --- markout ---------------------------------------------------------------


def test_markout_is_positive_when_the_market_moves_our_way() -> None:
    # Bought at 50c, mid rose to 52c: we bought well.
    report = compute_markout(
        [make_fill(action="buy", price=5000, offset=0.0)],
        series([(0.0, 5000), (5.0, 5200)]),
        horizons_seconds=(5.0,),
    )
    horizon = report.at(5.0)

    assert horizon is not None
    assert horizon.per_contract_ticks == 200.0
    assert not horizon.is_adverse


def test_markout_is_negative_when_we_are_picked_off() -> None:
    report = compute_markout(
        [make_fill(action="buy", price=5000, offset=0.0)],
        series([(0.0, 5000), (5.0, 4700)]),
        horizons_seconds=(5.0,),
    )
    horizon = report.at(5.0)

    assert horizon is not None
    assert horizon.per_contract_ticks == -300.0
    assert horizon.is_adverse
    assert "ADVERSE" in report.describe()


def test_markout_sign_flips_for_sells() -> None:
    # Sold at 50c and the mid fell to 48c: selling was right.
    report = compute_markout(
        [make_fill(action="sell", price=5000, offset=0.0)],
        series([(0.0, 5000), (5.0, 4800)]),
        horizons_seconds=(5.0,),
    )
    horizon = report.at(5.0)

    assert horizon is not None
    assert horizon.per_contract_ticks == 200.0


def test_markout_skips_fills_past_the_end_of_the_data() -> None:
    # Counting an unmeasurable markout as zero would drag the average toward
    # zero and hide adverse selection.
    report = compute_markout(
        [make_fill(offset=9.0)],
        series([(0.0, 5000), (10.0, 5000)]),
        horizons_seconds=(30.0,),
    )
    horizon = report.at(30.0)

    assert horizon is not None
    assert horizon.fill_count == 0
    assert report.skipped_fills == 1


def test_markout_weights_by_contract_count() -> None:
    report = compute_markout(
        [
            make_fill(action="buy", price=5000, count=1 * ONE, offset=0.0),
            make_fill(action="buy", price=5000, count=9 * ONE, offset=0.0),
        ],
        series([(0.0, 5000), (5.0, 5100)]),
        horizons_seconds=(5.0,),
    )
    horizon = report.at(5.0)

    assert horizon is not None
    assert horizon.fill_count == 2
    assert horizon.per_contract_ticks == 100.0


def test_mid_series_steps_rather_than_interpolating() -> None:
    mid_series = series([(0.0, 5000), (10.0, 6000)])["M1"]

    assert mid_series.mid_at(5.0) == 5000
    assert mid_series.mid_at(10.0) == 6000
    assert mid_series.mid_at(-1.0) is None
    assert mid_series.covers(10.0)
    assert not mid_series.covers(10.1)


def test_markout_buckets_by_time_to_close() -> None:
    fills = [
        (make_fill(action="buy", price=5000, offset=0.0, seconds_to_close=10.0), 10.0),
        (make_fill(action="buy", price=5000, offset=1.0, seconds_to_close=600.0), 600.0),
    ]
    # The mid must drop before the 15s horizon and the series must extend past
    # it, or the markout is unmeasurable rather than negative.
    buckets = markout_by_time_to_close(
        fills,
        series([(0.0, 5000), (1.0, 5000), (10.0, 4000), (30.0, 4000)]),
        horizon_seconds=15.0,
    )
    by_label = {bucket.label: bucket for bucket in buckets}

    assert by_label["final 30s"].fill_count == 1
    assert by_label["final 30s"].markout_ticks_per_contract < 0
    assert by_label["5m-15m"].fill_count == 1


def test_markout_buckets_drop_fills_with_no_close_time() -> None:
    buckets = markout_by_time_to_close(
        [(make_fill(offset=0.0), None)],
        series([(0.0, 5000), (20.0, 5000)]),
    )

    assert all(bucket.fill_count == 0 for bucket in buckets)


# --- performance -----------------------------------------------------------


def test_attribution_separates_spread_capture_from_inventory_drift() -> None:
    # Bought one contract 2c under the mid, and the mid then rose 10c.
    attribution = attribute_pnl(
        [make_fill(action="buy", price=4800, mid=5000, count=ONE)],
        final_position=ONE,
        final_mid=6000,
        fees_paid=0,
    )

    assert attribution.spread_capture == 200 * ONE
    assert attribution.inventory_pnl == 1000 * ONE
    assert attribution.net == 1200 * ONE


def test_attribution_reports_when_fees_exceed_gross() -> None:
    attribution = attribute_pnl(
        [make_fill(action="buy", price=4900, mid=5000, count=ONE)],
        final_position=ONE,
        final_mid=5000,
        fees_paid=40_000,
    )

    assert attribution.net < 0
    assert attribution.fee_share_of_gross is not None
    assert attribution.fee_share_of_gross > 1.0


def test_attribution_without_mid_credits_nothing_to_spread() -> None:
    attribution = attribute_pnl(
        [make_fill(action="buy", price=4800, mid=None)],
        final_position=ONE,
        final_mid=5000,
        fees_paid=0,
    )

    assert attribution.spread_capture == 0


def test_risk_metrics_measure_drawdown_from_the_peak() -> None:
    metrics = risk_metrics([(0.0, 0), (1.0, 500), (2.0, 100), (3.0, 300)])

    assert metrics.peak_equity == 500
    assert metrics.max_drawdown == 400
    assert metrics.final_equity == 300
    assert 0.0 < metrics.time_in_drawdown <= 1.0


def test_risk_metrics_handle_an_empty_curve() -> None:
    metrics = risk_metrics([])

    assert metrics.sample_count == 0
    assert metrics.sharpe is None


def test_sharpe_is_none_for_a_flat_curve() -> None:
    assert risk_metrics([(0.0, 10), (1.0, 10), (2.0, 10)]).sharpe is None


def test_fill_metrics_track_maker_share() -> None:
    metrics = fill_metrics(
        [
            make_fill(is_taker=False),
            make_fill(is_taker=False),
            make_fill(is_taker=True, action="sell"),
        ]
    )

    assert metrics.fill_count == 3
    assert metrics.maker_share == 2 / 3
    assert metrics.imbalance_contracts == 1.0


# --- screening -------------------------------------------------------------


def test_midpoint_market_is_structurally_unviable() -> None:
    # 2 cent spread at 50c: the round trip costs 3.5c. Impossible for anyone.
    score = score_market(MarketQuote("BTC", yes_bid=4900, yes_ask=5100, volume_24h=100_000))

    assert score.structurally_unviable
    assert score.net_edge_ticks < 0


def test_tail_market_with_the_same_spread_is_viable() -> None:
    score = score_market(MarketQuote("TAIL", yes_bid=400, yes_ask=800, volume_24h=1_000))

    assert not score.structurally_unviable
    assert score.net_edge_ticks > 0


def test_screen_ranks_by_expected_daily_value() -> None:
    report = screen_markets(
        [
            MarketQuote("A", yes_bid=4900, yes_ask=5100, volume_24h=1_000_000),
            MarketQuote("B", yes_bid=400, yes_ask=900, volume_24h=10_000),
            MarketQuote("C", yes_bid=400, yes_ask=900, volume_24h=1_000),
        ],
        min_volume_24h=0,
    )

    assert [score.ticker for score in report.viable] == ["B", "C"]
    assert report.unviable_count == 1


def test_screen_skips_one_sided_markets() -> None:
    report = screen_markets([MarketQuote("X", yes_bid=0, yes_ask=0, volume_24h=10)])

    assert report.skipped == 1
    assert report.scores == ()


def test_a_tick_wide_market_can_only_be_joined_not_improved() -> None:
    # There is nowhere to stand inside a one-tick spread, so the whole spread
    # is capturable. Subtracting a cent a side here would wrongly condemn most
    # of the exchange.
    tick_wide = MarketQuote("T", yes_bid=400, yes_ask=500, volume_24h=100)

    assert capturable_ticks(tick_wide, improvement_ticks=100) == 100


def test_improvement_leaves_at_least_one_tick_of_spread() -> None:
    two_cent = MarketQuote("T", yes_bid=400, yes_ask=600, volume_24h=100)

    # Improving one side by a cent leaves a one cent market, not a zero one.
    assert capturable_ticks(two_cent, improvement_ticks=100) == 100


def test_wide_markets_pay_the_full_improvement() -> None:
    wide = MarketQuote("T", yes_bid=3000, yes_ask=7000, volume_24h=100)

    assert capturable_ticks(wide, improvement_ticks=100) == 4000 - 200


def test_viable_band_widens_with_the_spread() -> None:
    narrow = viable_price_band(300)
    wide = viable_price_band(500)

    assert narrow is not None and wide is not None
    assert wide[1] > narrow[1]


def test_parse_market_converts_cents_to_ticks() -> None:
    quote = parse_market(
        {"ticker": "T", "yes_bid": 42, "yes_ask": 45, "volume_24h": 7, "series_ticker": "S"}
    )

    assert quote is not None
    assert quote.yes_bid == 4200
    assert quote.yes_ask == 4500
    assert quote.spread_ticks == 300
    assert quote.series == "S"


def test_parse_market_rejects_incomplete_entries() -> None:
    assert parse_market({"ticker": "T"}) is None


def test_series_breakdown_counts_viable_markets() -> None:
    report = screen_markets(
        [
            MarketQuote("A", yes_bid=400, yes_ask=900, volume_24h=100, series="TAILS"),
            MarketQuote("B", yes_bid=4900, yes_ask=5100, volume_24h=100, series="MIDS"),
        ],
        min_volume_24h=0,
    )

    assert report.by_series() == {"TAILS": (1, 1), "MIDS": (0, 1)}


# --- live API payload parsing ---------------------------------------------


def test_parse_market_reads_decimal_dollar_fields() -> None:
    # The live API sends decimal-dollar strings, not integer cents. Reading the
    # wrong field silently produced a zero bid and skipped every market.
    quote = parse_market(
        {
            "ticker": "T",
            "yes_bid_dollars": "0.4200",
            "yes_ask_dollars": "0.4500",
            "volume_24h_fp": "1234.00",
            "open_interest_fp": "500.00",
            "event_ticker": "E",
        }
    )

    assert quote is not None
    assert quote.yes_bid == 4200
    assert quote.yes_ask == 4500
    assert quote.volume_24h == 1234
    assert quote.open_interest == 500


def test_parse_market_reads_deci_cent_tick_size() -> None:
    quote = parse_market(
        {
            "ticker": "T",
            "yes_bid_dollars": "0.4200",
            "yes_ask_dollars": "0.4500",
            "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": "0.0010"}],
        }
    )

    assert quote is not None
    assert quote.tick_ticks == 10


def test_parse_market_defaults_to_a_cent_tick() -> None:
    quote = parse_market({"ticker": "T", "yes_bid_dollars": "0.42", "yes_ask_dollars": "0.45"})

    assert quote is not None
    assert quote.tick_ticks == 100


def test_parse_markets_drops_parlay_combos_by_default() -> None:
    raw = [
        {"ticker": "REAL", "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.60"},
        {
            "ticker": "COMBO",
            "yes_bid_dollars": "0.40",
            "yes_ask_dollars": "0.60",
            "mve_collection_ticker": "KXMVE-R",
        },
    ]

    assert [q.ticker for q in parse_markets(raw)] == ["REAL"]
    assert len(parse_markets(raw, skip_combos=False)) == 2


def test_parse_market_survives_malformed_numbers() -> None:
    assert parse_market({"ticker": "T", "yes_bid_dollars": "abc", "yes_ask_dollars": "0.5"}) is None


# --- price bands: the actionable screening rule -----------------------------


def test_distance_from_end_is_symmetric() -> None:
    assert distance_from_end(300) == 300
    assert distance_from_end(9700) == 300
    assert distance_from_end(5000) == 5000


def test_price_band_classifies_by_distance_not_side() -> None:
    # A 3c market and a 97c market are the same problem to a market maker.
    assert price_band(300) == price_band(9700)
    assert price_band(300) == "deep tail (<=5c from an end)"
    assert price_band(5000) == "near the money (30-50c)"


def test_by_price_band_separates_viable_tails_from_dead_middles() -> None:
    report = screen_markets(
        [
            MarketQuote("TAIL", yes_bid=250, yes_ask=350, volume_24h=1_000),
            MarketQuote("MID", yes_bid=4900, yes_ask=5100, volume_24h=100_000),
        ],
        min_volume_24h=0,
    )
    bands = by_price_band(report)

    # The tail is quotable on a 1c spread; the midpoint is not, at any volume.
    assert bands["deep tail (<=5c from an end)"]["viable"] == 1
    assert bands["near the money (30-50c)"]["viable"] == 0
    assert bands["near the money (30-50c)"]["volume"] == 100_000


# --- tapered tick size ------------------------------------------------------

TAPERED = [
    {"start": "0.0000", "end": "0.1000", "step": "0.0010"},
    {"start": "0.1000", "end": "0.9000", "step": "0.0100"},
    {"start": "0.9000", "end": "1.0000", "step": "0.0010"},
]


def test_tick_is_finer_in_the_tails_than_the_middle() -> None:
    # Kalshi's tapered_deci_cent: 0.1c in the tails, 1c in between. Treating
    # tick as a per-market constant is wrong in both directions.
    assert tick_at_price(TAPERED, 300) == 10
    assert tick_at_price(TAPERED, 5000) == 100
    assert tick_at_price(TAPERED, 9500) == 10


def test_parse_market_reads_the_tick_at_the_current_mid() -> None:
    tail = parse_market(
        {
            "ticker": "TAIL",
            "yes_bid_dollars": "0.0300",
            "yes_ask_dollars": "0.0400",
            "price_ranges": TAPERED,
        }
    )
    middle = parse_market(
        {
            "ticker": "MID",
            "yes_bid_dollars": "0.4900",
            "yes_ask_dollars": "0.5100",
            "price_ranges": TAPERED,
        }
    )

    assert tail is not None and middle is not None
    # An earlier version returned the first range's step for both, modelling
    # every midpoint market as ten times finer than it really is.
    assert tail.tick_ticks == 10
    assert middle.tick_ticks == 100


def test_a_fine_tick_lets_us_step_inside_a_one_cent_spread() -> None:
    tail = MarketQuote("T", yes_bid=300, yes_ask=400, volume_24h=100, tick_ticks=10)
    middle = MarketQuote("M", yes_bid=4900, yes_ask=5000, volume_24h=100, tick_ticks=100)

    # Same 1c spread. In the tail there are nine places to stand inside it, so
    # improving costs only a tenth of a cent; at the midpoint there are none.
    assert capturable_ticks(tail, improvement_ticks=100) == 10
    assert capturable_ticks(middle, improvement_ticks=100) == 100


def test_parse_market_reads_seconds_to_close_from_close_time() -> None:
    """Regression: this was hardcoded to None, silently disabling every
    time-to-close screen in suitability.py."""

    quote = parse_market(
        {
            "ticker": "KXBTC15M-TEST",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.47",
            "close_time": "2026-08-16T12:15:00Z",
        },
        now=datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC),
    )

    assert quote is not None
    assert quote.seconds_to_close == 900.0


def test_parse_market_reports_closed_market_as_unknown_not_negative() -> None:
    """A market that closed an hour ago must not look like one closing soon."""

    quote = parse_market(
        {
            "ticker": "KXBTC15M-PAST",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.47",
            "close_time": "2026-08-16T11:00:00Z",
        },
        now=datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC),
    )

    assert quote is not None
    assert quote.seconds_to_close is None


def test_parse_market_falls_back_to_expiration_time() -> None:
    quote = parse_market(
        {
            "ticker": "KXTEST",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.47",
            "expiration_time": "2026-08-16T13:00:00Z",
        },
        now=datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC),
    )

    assert quote is not None
    assert quote.seconds_to_close == 3600.0


def test_parse_market_survives_a_missing_or_unparseable_close_time() -> None:
    for raw in (
        {"ticker": "A", "yes_bid_dollars": "0.45", "yes_ask_dollars": "0.47"},
        {
            "ticker": "B",
            "yes_bid_dollars": "0.45",
            "yes_ask_dollars": "0.47",
            "close_time": "not a timestamp",
        },
    ):
        quote = parse_market(raw)
        assert quote is not None
        assert quote.seconds_to_close is None


def test_attribution_separates_a_directional_run_from_a_market_making_one() -> None:
    """The case that inverted a backtest ranking.

    A strategy that ends at its position limit can show a large mark to market
    while capturing almost no spread - the number is what the inventory did, not
    what the strategy earned. Attribution has to make that visible.
    """

    # Bought 10 contracts a full cent under the mid, then the mid ran up 10c.
    fills = [
        make_fill(action="buy", price=5000, count=10 * ONE, mid=5100),
    ]

    attribution = attribute_pnl(
        fills,
        fees_paid=0,
        final_marks={"M1": (10 * ONE, 6000)},
    )

    # One cent of edge on ten contracts.
    assert attribution.spread_capture == 100 * 10 * ONE
    # Everything above that is the market moving, not the quote being good.
    assert attribution.inventory_pnl > attribution.spread_capture
    assert attribution.net == attribution.spread_capture + attribution.inventory_pnl


def test_unscored_fills_fall_to_inventory_rather_than_inflating_capture() -> None:
    """No mid at fill time means we cannot claim the edge, so we do not."""

    fills = [make_fill(action="buy", price=5000, count=ONE, mid=None)]

    attribution = attribute_pnl(fills, fees_paid=0, final_marks={"M1": (ONE, 5100)})

    assert attribution.spread_capture == 0
    assert attribution.inventory_pnl > 0
