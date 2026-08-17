from kalshi_mm_bot.market.price import COUNT_SCALE, MONEY_SCALE
from kalshi_mm_bot.market.fees import (
    KalshiFeeModel,
    ZERO_FEE_MODEL,
    calibrate_from_fills,
)

HALF_DOLLAR = 5000
ONE_CONTRACT = COUNT_SCALE


def test_fee_matches_kalshi_published_example() -> None:
    # Kalshi's own worked example: one contract at $0.50 owes
    # 0.07 * 1 * 0.5 * 0.5 = $0.0175, billed as $0.02 after the per-order ceiling.
    model = KalshiFeeModel()

    exact = KalshiFeeModel(round_up_to_cent=False).fee_micros(
        yes_price=HALF_DOLLAR,
        count=ONE_CONTRACT,
    )
    billed = model.fee_micros(yes_price=HALF_DOLLAR, count=ONE_CONTRACT)

    assert exact == 17_500
    assert billed == 20_000


def test_fee_is_symmetric_about_fifty_cents() -> None:
    model = KalshiFeeModel(round_up_to_cent=False)
    count = 100 * COUNT_SCALE

    low = model.fee_micros(yes_price=2000, count=count)
    high = model.fee_micros(yes_price=8000, count=count)

    assert low == high


def test_ceiling_surcharge_shrinks_as_size_grows() -> None:
    model = KalshiFeeModel()

    small = model.breakeven_edge_ticks(yes_price=HALF_DOLLAR, count=ONE_CONTRACT)
    large = model.breakeven_edge_ticks(yes_price=HALF_DOLLAR, count=1000 * COUNT_SCALE)

    # The per-order ceiling is a fixed cost, so it dominates at one contract and
    # amortizes away by a thousand. This is why small live tests read worse than
    # the strategy actually is - and why they still cannot win at fifty cents.
    assert small > large
    assert large == 350  # 3.50 cents of round-trip edge needed at the midpoint


def test_round_trip_costs_two_ceilings() -> None:
    model = KalshiFeeModel()

    one_side = model.fee_micros(yes_price=HALF_DOLLAR, count=ONE_CONTRACT)
    round_trip = model.round_trip_micros(yes_price=HALF_DOLLAR, count=ONE_CONTRACT)

    assert round_trip == 2 * one_side


def test_minimum_viable_count_rejects_hopeless_edge() -> None:
    model = KalshiFeeModel()

    # Half a cent of edge cannot cover a 3.5 cent round trip at any size.
    assert (
        model.minimum_viable_count(
            yes_price=HALF_DOLLAR,
            edge_ticks=50,
            max_count=1000 * COUNT_SCALE,
        )
        is None
    )


def test_minimum_viable_count_finds_a_size_when_edge_is_sufficient() -> None:
    model = KalshiFeeModel()

    viable = model.minimum_viable_count(
        yes_price=HALF_DOLLAR,
        edge_ticks=400,
        max_count=100 * COUNT_SCALE,
    )

    assert viable is not None
    captured = 400 * viable
    assert captured >= model.round_trip_micros(yes_price=HALF_DOLLAR, count=viable)


def test_single_side_check_is_cheaper_than_round_trip() -> None:
    model = KalshiFeeModel()
    kwargs = {"yes_price": HALF_DOLLAR, "edge_ticks": 200, "max_count": 50 * COUNT_SCALE}

    round_trip = model.minimum_viable_count(**kwargs)
    one_side = model.minimum_viable_count(**kwargs, round_trip=False)

    assert one_side is not None
    assert round_trip is None or one_side <= round_trip


def test_edge_ticks_per_contract_covers_one_side_exactly() -> None:
    model = KalshiFeeModel()
    count = 1000 * COUNT_SCALE
    edge = model.edge_ticks_per_contract(HALF_DOLLAR)

    captured = edge * count
    owed = KalshiFeeModel(round_up_to_cent=False).fee_micros(
        yes_price=HALF_DOLLAR,
        count=count,
    )

    assert captured >= owed


def test_tails_are_dramatically_cheaper_than_the_midpoint() -> None:
    model = KalshiFeeModel()
    count = 100 * COUNT_SCALE

    tail = model.breakeven_edge_ticks(yes_price=500, count=count)
    middle = model.breakeven_edge_ticks(yes_price=HALF_DOLLAR, count=count)

    # 5 cents versus 50 cents: the fee is proportional to P*(1-P), which is the
    # entire reason a Kalshi market maker should be quoting the tails.
    assert tail * 4 < middle


def test_zero_fee_model_charges_nothing() -> None:
    assert ZERO_FEE_MODEL.fee_micros(yes_price=HALF_DOLLAR, count=ONE_CONTRACT) == 0


def test_maker_schedule_bypasses_the_formula_when_enabled() -> None:
    model = KalshiFeeModel(
        charge_makers_taker_rate=False,
        maker_fee_per_contract_micros=2_500,
    )
    count = 100 * COUNT_SCALE

    maker = model.fee_micros(yes_price=HALF_DOLLAR, count=count, is_taker=False)
    taker = model.fee_micros(yes_price=HALF_DOLLAR, count=count, is_taker=True)

    assert maker == 250_000
    assert taker > maker


def test_calibration_detects_a_wrong_schedule() -> None:
    model = KalshiFeeModel()
    # Real fills that were actually billed a flat quarter cent per contract.
    fills = [(HALF_DOLLAR, ONE_CONTRACT, False, 2_500)] * 4

    calibration = calibrate_from_fills(model, fills)

    assert calibration.sample_count == 4
    assert not calibration.matches
    assert calibration.error_micros > 0
    assert "MISMATCH" in calibration.describe()


def test_calibration_confirms_a_correct_schedule() -> None:
    model = KalshiFeeModel()
    billed = model.fee_micros(yes_price=3000, count=5 * COUNT_SCALE)

    calibration = calibrate_from_fills(model, [(3000, 5 * COUNT_SCALE, True, billed)])

    assert calibration.matches


def test_fee_is_expressed_in_money_scale() -> None:
    model = KalshiFeeModel(round_up_to_cent=False)
    # 0.07 * 100 contracts * 0.5 * 0.5 = $1.75
    fee = model.fee_micros(yes_price=HALF_DOLLAR, count=100 * COUNT_SCALE)

    assert fee == int(1.75 * MONEY_SCALE)


def test_calibrate_accepts_the_shape_calibrate_fees_writes() -> None:
    """The probe script writes dicts; requiring tuples broke the loop it exists
    to close."""

    from kalshi_mm_bot.market.fees import KalshiFeeModel, calibrate_from_fills

    fills = [
        {"yes_price": 5700, "count": 100, "is_taker": False, "fee_micros": 0},
        {"yes_price": 6200, "count": 100, "is_taker": False, "fee_micros": 0},
    ]

    calibration = calibrate_from_fills(KalshiFeeModel(), fills)

    assert calibration.sample_count == 2
    assert calibration.actual_micros == 0
    # The default model charges makers, so this is the mismatch the live probe
    # actually found on KXBTC15M.
    assert calibration.modelled_micros > 0


def test_calibrate_still_accepts_tuples() -> None:
    from kalshi_mm_bot.market.fees import KalshiFeeModel, calibrate_from_fills

    calibration = calibrate_from_fills(
        KalshiFeeModel(), [(5700, 100, False, 0), (6200, 100, False, 0)]
    )

    assert calibration.sample_count == 2
    assert calibration.actual_micros == 0
