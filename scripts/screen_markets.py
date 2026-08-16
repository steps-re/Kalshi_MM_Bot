"""Rank live Kalshi markets by whether market making in them can pay its fees.

    python scripts/screen_markets.py --prod --min-volume 500

Reads the exchange, scores every two-sided market, and prints the ones where
the spread actually covers the round-trip fee. Also prints the structural
picture - what fraction of the exchange is unquotable at current prices - which
is the number that decides whether this strategy has a business at all.

Sends no orders and needs only read access.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.analytics.screening import (  # noqa: E402
    DEFAULT_ASSUMED_SIZE,
    DEFAULT_IMPROVEMENT_TICKS,
    MarketQuote,
    describe_series,
    parse_markets,
    screen_markets,
    viable_price_band,
)
from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.rest import KalshiRestClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402
from kalshi_mm_bot.market.price import COUNT_SCALE, ONE_DOLLAR  # noqa: E402
from kalshi_mm_bot.market.fees import KalshiFeeModel  # noqa: E402


async def fetch_open_markets(rest: KalshiRestClient, *, max_pages: int) -> list[dict]:
    """Page through open markets. Kalshi caps a page at 1000 entries."""

    markets: list[dict] = []
    cursor: str | None = None

    for _ in range(max_pages):
        page, cursor = await rest.list_markets(status="open", cursor=cursor)
        markets.extend(page)

        if not cursor or not page:
            break

    return markets


def print_structural_picture(fee_model: KalshiFeeModel, improvement_ticks: int) -> None:
    print("Where the arithmetic works (improving the touch by "
          f"{improvement_ticks / 100:.0f}c per side):")
    print(f"  {'spread':>7}  {'viable YES price band':<40}")

    for spread_cents in (1, 2, 3, 4, 5, 6, 8, 10):
        band = viable_price_band(
            spread_cents * 100,
            fee_model=fee_model,
            improvement_ticks=improvement_ticks,
        )

        if band is None:
            print(f"  {spread_cents:>6}c  none at any price")
            continue

        low, high = band

        if high >= ONE_DOLLAR // 2 - 100:
            print(f"  {spread_cents:>6}c  the whole range")
        else:
            print(
                f"  {spread_cents:>6}c  ${low / ONE_DOLLAR:.2f}-${high / ONE_DOLLAR:.2f}"
                f"  or  ${(ONE_DOLLAR - high) / ONE_DOLLAR:.2f}-"
                f"${(ONE_DOLLAR - low) / ONE_DOLLAR:.2f}"
            )

    print()


async def _run(args: argparse.Namespace) -> None:
    fee_model = KalshiFeeModel(trading_fee_bps=args.fee_bps)

    print_structural_picture(fee_model, args.improvement_ticks)

    if args.from_json:
        raw_markets = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
    else:
        settings = load_settings()
        environment = settings.environment(prod=args.prod)
        auth = KalshiAuth(settings.api_key_id, settings.private_key_path)
        rest = KalshiRestClient(environment.rest_base_url, auth)

        try:
            print(f"Fetching open markets from {environment.name}...")
            raw_markets = await fetch_open_markets(rest, max_pages=args.max_pages)
        finally:
            await rest.close()

    if args.save_json:
        Path(args.save_json).write_text(json.dumps(raw_markets), encoding="utf-8")
        print(f"Saved {len(raw_markets)} raw market(s) to {args.save_json}")

    quotes: tuple[MarketQuote, ...] = parse_markets(
        raw_markets,
        skip_combos=not args.include_combos,
    )
    report = screen_markets(
        quotes,
        fee_model=fee_model,
        improvement_ticks=args.improvement_ticks,
        participation_share=args.participation,
        assumed_size=args.size * COUNT_SCALE,
        min_volume_24h=args.min_volume,
    )

    print(report.describe(limit=args.limit))
    print()
    print(describe_series(report))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prod", action="store_true", help="Use the production environment.")
    parser.add_argument("--limit", type=int, default=25, help="How many markets to list.")
    parser.add_argument(
        "--min-volume",
        type=int,
        default=100,
        help="Skip markets under this 24h contract volume. Default: 100.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_ASSUMED_SIZE // COUNT_SCALE,
        help="Order size in contracts to price the per-order fee ceiling against.",
    )
    parser.add_argument(
        "--improvement-ticks",
        type=int,
        default=DEFAULT_IMPROVEMENT_TICKS,
        help="Ticks we give up per side to get filled. 100 = one cent.",
    )
    parser.add_argument(
        "--participation",
        type=float,
        default=0.10,
        help="Share of 24h volume we assume we could intermediate.",
    )
    parser.add_argument("--fee-bps", type=int, default=700, help="Trading fee rate in bps.")
    parser.add_argument("--max-pages", type=int, default=20, help="Market pages to fetch.")
    parser.add_argument(
        "--include-combos",
        action="store_true",
        help="Include multivariate parlay markets. There are tens of thousands "
        "and essentially none are quoted; excluded by default.",
    )
    parser.add_argument("--save-json", help="Write the raw market payload here.")
    parser.add_argument("--from-json", help="Score a saved payload instead of calling the API.")
    return parser.parse_args()


def main() -> None:
    try:
        asyncio.run(_run(_parse_args()))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
