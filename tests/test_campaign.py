"""Campaign premise guards.

Most of these encode a failure that actually happened today, so they are less
about coverage than about making sure the same class of mistake cannot pass
this monitor.
"""

import pytest

from kalshi_mm_bot.live.campaign import (
    CampaignLimits,
    CampaignMonitor,
    CampaignSample,
    Fill,
    Verdict,
    fills_from_ledger,
)
from kalshi_mm_bot.market.price import COUNT_SCALE

ONE = COUNT_SCALE
HOUR = 3600.0


def maker(fee_micros=0, price=5000, mid_at_fill=None):
    return Fill(
        yes_price=price,
        count=ONE,
        is_taker=False,
        fee_micros=fee_micros,
        mid_at_fill=mid_at_fill,
    )


def taker(fee_micros=17_500, price=5000):
    return Fill(yes_price=price, count=ONE, is_taker=True, fee_micros=fee_micros)


def sample(fills, *, elapsed=600.0, quotes=100, balance=50_000_000, pnl=0):
    return CampaignSample(
        fills=fills,
        balance_micros=balance,
        realized_pnl_micros=pnl,
        elapsed_seconds=elapsed,
        quotes_placed=quotes,
    )


def reading(verdict_obj, key):
    return next(r for r in verdict_obj.readings if r.key == key)


def test_healthy_campaign_keeps_running() -> None:
    fills = [maker(mid_at_fill=5020) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills))

    assert not verdict.should_halt
    assert reading(verdict, "maker_fee").verdict is Verdict.OK


def test_a_maker_fee_appearing_halts_the_campaign() -> None:
    """The premise the whole strategy rests on."""

    fills = [maker(fee_micros=17_500, mid_at_fill=5020) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills))

    assert verdict.should_halt
    assert reading(verdict, "maker_fee").verdict is Verdict.TRIPPED


def test_dust_sized_maker_fee_does_not_halt_a_healthy_campaign() -> None:
    """Measured: 40 of 41 maker fills free, one charged $0.000050.

    A strict-zero threshold would halt over five millionths of a dollar against
    a +0.20c median edge, and a monitor that cries wolf gets switched off.
    """

    fills = [maker(mid_at_fill=5020) for _ in range(40)]
    fills.append(maker(fee_micros=50, price=9130, mid_at_fill=9140))
    fills.append(taker())

    verdict = CampaignMonitor().assess(sample(fills))
    maker_reading = reading(verdict, "maker_fee")

    assert not verdict.should_halt
    assert maker_reading.verdict is Verdict.OK
    # Still surfaced, because the first sign of a schedule change is a small one.
    assert "watch it" in maker_reading.detail


def test_an_unreadable_fee_halts_rather_than_counting_as_free() -> None:
    """The bug this monitor exists to catch."""

    fills = [maker(mid_at_fill=5020) for _ in range(25)]
    fills.append(maker(fee_micros=None))
    fills.append(taker())

    verdict = CampaignMonitor().assess(sample(fills))

    assert verdict.should_halt
    assert reading(verdict, "maker_fee").verdict is Verdict.UNKNOWN


def test_free_makers_prove_nothing_when_takers_also_read_free() -> None:
    """A broken reader and a free market are the same observation."""

    fills = [maker(mid_at_fill=5020) for _ in range(25)] + [taker(fee_micros=0)]
    verdict = CampaignMonitor().assess(sample(fills))

    control = reading(verdict, "fee_reader_control")
    assert control.verdict is Verdict.UNKNOWN
    assert verdict.should_halt


def test_missing_control_is_pending_early_and_unknown_later() -> None:
    """You do not get to stay unmeasured forever while spending money."""

    fills = [maker(mid_at_fill=5020) for _ in range(25)]

    early = CampaignMonitor().assess(sample(fills, elapsed=600.0))
    late = CampaignMonitor().assess(sample(fills, elapsed=2 * HOUR))

    assert reading(early, "fee_reader_control").verdict is Verdict.PENDING
    assert not early.should_halt

    assert reading(late, "fee_reader_control").verdict is Verdict.UNKNOWN
    assert late.should_halt


def test_adverse_selection_halts_the_campaign() -> None:
    # Bought at 0.50, mid fell to 0.49 every time.
    fills = [maker(mid_at_fill=4900) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills))

    assert verdict.should_halt
    assert reading(verdict, "markout").verdict is Verdict.TRIPPED


def test_a_maker_rebate_is_reported_as_an_opportunity_not_a_halt() -> None:
    """Exchanges pay makers to show up. Nobody can act on a window they cannot see."""

    fills = [maker(fee_micros=-500, mid_at_fill=5020) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills))

    assert not verdict.should_halt
    assert reading(verdict, "maker_fee").verdict is Verdict.FAVOURABLE
    assert verdict.opportunities
    assert "will not last" in verdict.describe()


def test_unusually_good_markout_is_flagged_as_an_opportunity() -> None:
    fills = [maker(mid_at_fill=5200) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills))

    assert reading(verdict, "markout").verdict is Verdict.FAVOURABLE
    assert not verdict.should_halt


def test_an_opportunity_never_masks_a_halt() -> None:
    """A rebate does not make a broken premise acceptable."""

    fills = [maker(fee_micros=-500, mid_at_fill=4900) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills))

    assert verdict.should_halt  # markout is still tripped
    assert verdict.opportunities
    assert "HALT" in verdict.describe()


def test_queue_crowding_shows_up_as_a_fill_rate_trip() -> None:
    fills = [maker(mid_at_fill=5020) for _ in range(25)] + [taker()]
    verdict = CampaignMonitor().assess(sample(fills, quotes=10_000))

    assert reading(verdict, "fill_rate").verdict is Verdict.TRIPPED


def test_a_configured_floor_with_no_reading_halts() -> None:
    """A limit you cannot evaluate is not a limit."""

    limits = CampaignLimits(min_balance_micros=10_000_000)
    fills = [maker(mid_at_fill=5020) for _ in range(25)] + [taker()]

    verdict = CampaignMonitor(limits=limits).assess(
        CampaignSample(
            fills=fills,
            balance_micros=None,
            realized_pnl_micros=0,
            elapsed_seconds=600.0,
            quotes_placed=100,
        )
    )

    assert verdict.should_halt
    assert reading(verdict, "balance").verdict is Verdict.UNKNOWN


def test_halt_latches_until_a_person_clears_it() -> None:
    monitor = CampaignMonitor()
    bad = [maker(fee_micros=17_500, mid_at_fill=5020) for _ in range(25)] + [taker()]
    good = [maker(mid_at_fill=5020) for _ in range(25)] + [taker()]

    monitor.assess(sample(bad))
    assert monitor.halted

    monitor.assess(sample(good))
    assert monitor.halted, "a halt must not clear itself just because the next window looks fine"

    monitor.clear_halt()
    assert not monitor.halted


def test_ledger_fills_carry_unreadable_fees_through_as_none() -> None:
    """The strict reader must reach the monitor intact."""

    built = fills_from_ledger(
        [
            {"yes_price_dollars": "0.50", "count_fp": "1.00", "is_taker": False,
             "fee_cost": "0.000000"},
            {"yes_price_dollars": "0.50", "count_fp": "1.00", "is_taker": True},
        ]
    )

    assert built[0].fee_micros == 0
    assert built[1].fee_micros is None


def test_limits_reject_a_nonsensical_opportunity_threshold() -> None:
    with pytest.raises(ValueError):
        CampaignLimits(min_mean_markout_cents=1.0, good_mean_markout_cents=0.5)


def test_markout_respects_trade_direction() -> None:
    """A sell scored with the buy convention reads exactly backwards."""

    rise_after_buy = Fill(
        yes_price=5000, count=ONE, is_taker=False, fee_micros=0,
        mid_at_fill=5100, action="buy",
    )
    rise_after_sell = Fill(
        yes_price=5000, count=ONE, is_taker=False, fee_micros=0,
        mid_at_fill=5100, action="sell",
    )

    assert rise_after_buy.markout_cents == 1.0
    assert rise_after_sell.markout_cents == -1.0


def test_a_losing_two_sided_book_is_not_reported_as_flat() -> None:
    """Buys and sells both picked off must not cancel into a healthy average."""

    fills = []
    for _ in range(13):
        fills.append(Fill(5000, ONE, False, 0, mid_at_fill=4900, action="buy"))
        fills.append(Fill(5000, ONE, False, 0, mid_at_fill=5100, action="sell"))
    fills.append(taker())

    verdict = CampaignMonitor().assess(sample(fills))

    assert reading(verdict, "markout").verdict is Verdict.TRIPPED
    assert verdict.should_halt


def test_ledger_fills_carry_the_action_through() -> None:
    built = fills_from_ledger(
        [{"yes_price_dollars": "0.50", "count_fp": "1.00", "is_taker": False,
          "fee_cost": "0.000000", "action": "sell"}]
    )

    assert built[0].action == "sell"


def test_a_pure_maker_run_is_told_how_to_satisfy_the_control() -> None:
    """It cannot pass from its own flow, so the message must say what to do."""

    fills = [maker(mid_at_fill=5020) for _ in range(25)]
    verdict = CampaignMonitor().assess(sample(fills, elapsed=600.0))

    detail = reading(verdict, "fee_reader_control").detail
    assert "cross" in detail
