from kalshi_mm_bot.analytics.competition import (
    QuoteEpisode,
    analyse_competition,
    analyse_toxicity,
    spread_impact,
)
from kalshi_mm_bot.market.price import COUNT_SCALE
from kalshi_mm_bot.market.series import MidSeries
from kalshi_mm_bot.research.assumptions import (
    Assumption,
    AssumptionLedger,
    Measurement,
    Verdict,
    default_ledger,
)
from kalshi_mm_bot.research.measure import (
    capital_required,
    measure_edge_cap,
    measure_fill_rate,
    measure_maker_fee,
    measure_participation,
    measure_spread_capture,
)
from kalshi_mm_bot.sim.fills import SimulatedFill

NOW = "2026-08-16T20:00:00Z"
ONE = COUNT_SCALE


def fill(*, action="buy", price=5000, count=ONE, offset=0.0, mid=None, ticker="M1", fid="f1"):
    return SimulatedFill(
        fill_id=fid,
        order_id="o1",
        market_ticker=ticker,
        action=action,
        side="yes",
        yes_price=price,
        count=count,
        offset_seconds=offset,
        observed_at_utc=None,
        fill_model="queue",
        reason="same_level_reduction",
        is_taker=False,
        mid_at_fill=mid,
    )


def series(points, ticker="M1"):
    return {
        ticker: MidSeries(
            market_ticker=ticker,
            offsets=tuple(o for o, _ in points),
            mids=tuple(m for _, m in points),
        )
    }


# --- the ledger -----------------------------------------------------------


def test_unmeasured_is_never_a_pass() -> None:
    # Silence is not evidence. This is the whole point of the ledger.
    ledger = default_ledger()

    assert all(f.verdict is Verdict.UNMEASURED for f in ledger.findings())
    assert not ledger.ready_to_trade
    assert {f.assumption.key for f in ledger.blocking_unresolved} == {
        "maker_fee",
        "spread_capture",
    }


def test_a_thin_sample_is_insufficient_not_confirmed() -> None:
    ledger = default_ledger()
    # Bang on the assumed value, but from far too few observations.
    ledger.record(Measurement("spread_capture", 0.30, sample_size=5, measured_at_utc=NOW))

    assert ledger.finding("spread_capture").verdict is Verdict.INSUFFICIENT
    assert not ledger.ready_to_trade


def test_reality_worse_than_assumed_reads_optimistic() -> None:
    ledger = default_ledger()
    ledger.record(Measurement("spread_capture", 0.10, sample_size=500, measured_at_utc=NOW))

    finding = ledger.finding("spread_capture")

    assert finding.verdict is Verdict.OPTIMISTIC
    assert finding.error is not None and finding.error < 0
    assert "!!" in finding.describe()


def test_reality_better_than_assumed_reads_conservative() -> None:
    ledger = default_ledger()
    ledger.record(Measurement("spread_capture", 0.60, sample_size=500, measured_at_utc=NOW))

    assert ledger.finding("spread_capture").verdict is Verdict.CONSERVATIVE


def test_within_tolerance_confirms() -> None:
    ledger = default_ledger()
    ledger.record(Measurement("spread_capture", 0.28, sample_size=500, measured_at_utc=NOW))

    assert ledger.finding("spread_capture").verdict is Verdict.CONFIRMED


def test_ready_to_trade_needs_every_blocking_assumption() -> None:
    ledger = default_ledger()
    ledger.record(Measurement("spread_capture", 0.30, sample_size=500, measured_at_utc=NOW))

    # maker_fee still unmeasured
    assert not ledger.ready_to_trade

    ledger.record(Measurement("maker_fee", 0.0, sample_size=100, measured_at_utc=NOW))

    assert ledger.ready_to_trade


def test_a_nonzero_maker_fee_blocks_when_zero_was_assumed() -> None:
    ledger = default_ledger()
    ledger.record(Measurement("maker_fee", 1750.0, sample_size=100, measured_at_utc=NOW))

    assert ledger.finding("maker_fee").verdict is Verdict.OPTIMISTIC
    assert not ledger.ready_to_trade


def test_recording_an_unregistered_measurement_is_rejected() -> None:
    ledger = AssumptionLedger()

    try:
        ledger.record(Measurement("invented", 1.0, 100, NOW))
    except KeyError as error:
        assert "invented" in str(error)
    else:
        raise AssertionError("expected KeyError")


def test_zero_tolerance_assumption_rejects_any_drift() -> None:
    ledger = AssumptionLedger()
    ledger.register(
        Assumption(
            key="exact",
            statement="must be zero",
            assumed=0.0,
            unit="",
            how_to_measure="-",
            min_samples=1,
        )
    )
    ledger.record(Measurement("exact", 0.5, sample_size=10, measured_at_utc=NOW))

    assert ledger.finding("exact").verdict is Verdict.OPTIMISTIC


# --- measurement adapters -------------------------------------------------


def test_maker_fee_measurement_ignores_taker_fills() -> None:
    # (yes_price, count, is_taker, actual_fee_micros)
    measurement = measure_maker_fee(
        [(5000, ONE, True, 20_000), (5000, ONE, False, 0)],
        measured_at_utc=NOW,
    )

    assert measurement.sample_size == 1
    assert measurement.observed == 0.0


def test_maker_fee_measurement_reports_a_real_charge() -> None:
    measurement = measure_maker_fee(
        [(5000, 2 * ONE, False, 8_000)] * 3,
        measured_at_utc=NOW,
    )

    # 8,000 micros over 2 contracts, three times = 4,000 micros per contract.
    assert measurement.observed == 4_000
    assert measurement.sample_size == 3


def test_spread_capture_is_one_when_the_market_does_not_move() -> None:
    measurement = measure_spread_capture(
        [fill(action="buy", price=4800, mid=5000, offset=0.0)],
        series([(0.0, 5000), (60.0, 5000)]),
        measured_at_utc=NOW,
    )

    assert measurement.observed == 1.0
    assert measurement.sample_size == 1


def test_spread_capture_falls_when_the_market_runs_away() -> None:
    # Bought 2c under mid, then mid dropped 1c: half the edge survived. The
    # move must land before the 30s horizon, and the series must extend past it.
    measurement = measure_spread_capture(
        [fill(action="buy", price=4800, mid=5000, offset=0.0)],
        series([(0.0, 5000), (10.0, 4900), (60.0, 4900)]),
        measured_at_utc=NOW,
    )

    assert measurement.observed == 0.5


def test_spread_capture_clamps_underwater_fills_at_zero() -> None:
    # One disaster must not net against other fills into a flattering mean.
    measurement = measure_spread_capture(
        [fill(action="buy", price=4800, mid=5000, offset=0.0)],
        series([(0.0, 5000), (10.0, 3000), (60.0, 3000)]),
        measured_at_utc=NOW,
    )

    assert measurement.observed == 0.0


def test_spread_capture_skips_fills_without_forward_data() -> None:
    measurement = measure_spread_capture(
        [fill(offset=100.0, mid=5000)],
        series([(0.0, 5000), (10.0, 5000)]),
        measured_at_utc=NOW,
    )

    assert measurement.sample_size == 0


def test_participation_uses_the_median_not_the_aggregate() -> None:
    # One market where we were nearly the only participant must not carry it.
    measurement = measure_participation(
        {"A": 10, "B": 10, "C": 900},
        {"A": 1000, "B": 1000, "C": 1000},
        measured_at_utc=NOW,
    )

    assert measurement.observed == 0.01
    assert measurement.sample_size == 3


def test_fill_rate_is_filled_over_placed() -> None:
    measurement = measure_fill_rate(orders_placed=200, orders_filled=30, measured_at_utc=NOW)

    assert measurement.observed == 0.15
    assert measurement.sample_size == 200


def test_edge_cap_reports_the_top_decile() -> None:
    fills = [fill(price=5000, mid=5000 + i, fid=f"f{i}") for i in range(1, 21)]

    measurement = measure_edge_cap(fills, measured_at_utc=NOW)

    # Best two of twenty are 20 and 19 ticks -> 0.195c
    assert round(measurement.observed, 3) == 0.195
    assert measurement.sample_size == 20


def test_capital_scales_inversely_with_turnover() -> None:
    slow = capital_required(contracts_per_day=100_000, turns_per_day=1)
    fast = capital_required(contracts_per_day=100_000, turns_per_day=10)

    assert slow == 100_000
    assert fast == 10_000


# --- competition ----------------------------------------------------------


def episode(**kw):
    base = dict(
        market_ticker="M1",
        side="bid",
        our_price=4000,
        started_at=0.0,
        ended_at=10.0,
        best_at_start=4000,
        best_at_end=4000,
    )
    base.update(kw)
    return QuoteEpisode(**base)


def test_competition_reports_undercut_rate_and_speed() -> None:
    report = analyse_competition(
        [
            episode(undercut_at=2.0),
            episode(undercut_at=4.0),
            episode(),
        ]
    )

    assert report.episodes == 3
    assert round(report.undercut_rate, 2) == 0.67
    assert report.median_seconds_to_undercut == 3.0


def test_competition_flags_when_most_quotes_are_stepped_inside() -> None:
    report = analyse_competition([episode(undercut_at=1.0) for _ in range(5)])

    assert "stepped inside" in report.describe()


def test_touch_move_measures_only_improvement_past_us() -> None:
    improved = episode(best_at_end=4200)
    retreated = episode(best_at_end=3800)

    assert improved.touch_moved_against_us == 200
    assert retreated.touch_moved_against_us == 0


def test_toxicity_separates_concentrated_from_diffuse() -> None:
    # 30 fills, one catastrophic: that is being picked off, not drift.
    fills = [fill(fid=f"f{i}", price=5000) for i in range(30)]
    forward = {f"f{i}": 4999 for i in range(29)}
    forward["f29"] = 3000

    report = analyse_toxicity(fills, forward)

    assert report.is_concentrated
    assert "CONCENTRATED" in report.describe()


def test_toxicity_reads_diffuse_when_losses_are_spread_evenly() -> None:
    fills = [fill(fid=f"f{i}", price=5000) for i in range(30)]
    forward = {f"f{i}": 4900 for i in range(30)}

    report = analyse_toxicity(fills, forward)

    assert not report.is_concentrated
    assert "diffuse" in report.describe()


def test_spread_impact_detects_our_own_compression() -> None:
    change, verdict = spread_impact(before=[800] * 10, during=[300] * 10)

    assert change < -0.15
    assert "collapses" in verdict


def test_spread_impact_accepts_a_surviving_spread() -> None:
    change, verdict = spread_impact(before=[800] * 10, during=[780] * 10)

    assert "survives" in verdict


def test_spread_impact_needs_both_windows() -> None:
    _, verdict = spread_impact(before=[], during=[300])

    assert "not enough observations" in verdict
