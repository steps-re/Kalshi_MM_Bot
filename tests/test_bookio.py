"""The two orderbook conventions, pinned with payloads observed live.

Getting these crossed produced three broken analyses in one day. Each test uses
the real shape from the feed it names.
"""

from kalshi_mm_bot.market.bookio import rest_top, ws_top
from kalshi_mm_bot.market.price import parse_price_fp

# Verbatim shape from GET /markets/KXBTC15M-.../orderbook on 2026-08-17:
# yes bid 0.64, yes ask 0.65 - and the ask arrives as a NO price of 0.34/0.35.
REST_PAYLOAD = {
    "no_dollars": [["0.3400", "1745.82"], ["0.3500", "0.02"]],
    "yes_dollars": [["0.6300", "4357.08"], ["0.6400", "3888.58"]],
}

# Verbatim shape from a websocket orderbook_snapshot the same day: the no side
# is ALREADY in YES price space - best ask 0.45 against best bid 0.44, with the
# deep 0.999 level being a far out-of-the-money ask, not an impossible NO bid.
WS_PAYLOAD = {
    "yes_dollars_fp": [["0.4300", "1238.89"], ["0.4400", "1489.62"]],
    "no_dollars_fp": [["0.4500", "2694.68"], ["0.9990", "59119.00"]],
}


def test_rest_no_side_is_folded_about_a_dollar() -> None:
    top = rest_top(REST_PAYLOAD)

    assert top is not None
    assert top.bid == parse_price_fp("0.6400")
    # Best ask = 1 - HIGHEST no price: 1 - 0.35 = 0.65, matching the market's
    # own quoted ask. (First draft of this test folded the lowest NO level and
    # expected 0.66 - the convention is subtle even when writing its test.)
    assert top.ask == parse_price_fp("0.6500")
    assert top.ask_size == 0.02
    assert top.bid_size == 3888.58


def test_ws_no_side_is_already_yes_asks() -> None:
    top = ws_top(WS_PAYLOAD)

    assert top is not None
    assert top.bid == parse_price_fp("0.4400")
    assert top.ask == parse_price_fp("0.4500")
    assert top.ask_size == 2694.68


def test_applying_the_wrong_convention_crosses_the_book() -> None:
    """The failure signature: repeated None is a convention error, not quiet."""

    # Folding the WS payload as if it were REST puts an "ask" at 1-0.999=0.001,
    # under the 0.44 bid.
    assert rest_top(WS_PAYLOAD) is None


def test_one_sided_and_empty_books_return_none() -> None:
    assert rest_top({"yes_dollars": [["0.5000", "1.00"]]}) is None
    assert ws_top({}) is None


def test_malformed_levels_are_skipped_not_fatal() -> None:
    top = ws_top(
        {
            "yes_dollars_fp": [["0.4400", "10.00"], ["bad", None], []],
            "no_dollars_fp": [["0.4500", "5.00"]],
        }
    )

    assert top is not None
    assert top.bid == parse_price_fp("0.4400")
