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
    parse_market,
    screen_markets,
    score_market,
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


def test_viable_band_is_empty_for_a_two_cent_spread() -> None:
    # Improving a tick per side leaves nothing to capture.
    assert viable_price_band(200) is None


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
