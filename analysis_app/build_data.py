"""Regenerate the app's `data/scan.json` from a raw Kalshi market payload.

    python scripts/screen_markets.py --prod --save-json /tmp/markets.json
    python analysis_app/build_data.py /tmp/markets.json

Kept as a script rather than done by hand so the published figures can always
be re-derived, and so a stale scan is a matter of re-running one command.

Scan the WHOLE exchange. An earlier version of this analysis stopped paging
early, missed every crypto market, and then reported their absence as a
finding. `scripts/screen_markets.py` pages to exhaustion; do not cap it.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kalshi_mm_bot.analytics.screening import (  # noqa: E402
    DEFAULT_IMPROVEMENT_TICKS,
    PRICE_BANDS,
    capturable_ticks,
    by_price_band,
    parse_markets,
    price_band,
    score_market,
    screen_markets,
)
from kalshi_mm_bot.market.fees import KalshiFeeModel  # noqa: E402
from kalshi_mm_bot.market.price import ONE_DOLLAR  # noqa: E402

MIN_VOLUME = 50
BASE_FEE = KalshiFeeModel()
MAKER_FEE = KalshiFeeModel(charge_makers_taker_rate=False, maker_fee_per_contract_micros=2_500)
NO_FEE = KalshiFeeModel(trading_fee_bps=0, round_up_to_cent=False)


def family(ticker: str) -> str:
    return ticker.split("-")[0]


def category_index(raw_markets: list[dict]) -> dict[str, str]:
    """Kalshi's own category, carried through from the /events payload."""

    return {m["ticker"]: (m.get("_category") or "Uncategorized") for m in raw_markets}


def build(raw_markets: list[dict], *, scanned_at_utc: str, records: int) -> dict:
    quotes = parse_markets(raw_markets)
    combos = records - len(raw_markets)
    report = screen_markets(quotes, min_volume_24h=MIN_VOLUME)
    liquid = [q for q in quotes if q.is_quotable and q.volume_24h >= MIN_VOLUME]
    viable = report.viable

    families: dict[str, dict] = collections.defaultdict(
        lambda: {"viable": 0, "total": 0, "daily": 0.0, "volume": 0}
    )
    for score in report.scores:
        entry = families[family(score.ticker)]
        entry["total"] += 1
        entry["volume"] += score.market.volume_24h
        if not score.structurally_unviable:
            entry["viable"] += 1
            entry["daily"] += score.expected_daily_micros / 1e6

    def totals(fee_model: KalshiFeeModel, share: float) -> dict:
        scored = [
            score_market(q, fee_model=fee_model, participation_share=share) for q in liquid
        ]
        ok = [s for s in scored if not s.structurally_unviable]
        return {
            "viable": len(ok),
            "volume": sum(s.market.volume_24h for s in ok),
            "daily": round(sum(s.expected_daily_micros for s in ok) / 1e6, 2),
        }

    bands = by_price_band(report)
    categories: dict[str, dict] = collections.defaultdict(
        lambda: {"viable": 0, "total": 0, "daily": 0.0, "volume": 0}
    )
    cat_of = category_index(raw_markets)
    for score in report.scores:
        entry = categories[cat_of.get(score.ticker, "Uncategorized")]
        entry["total"] += 1
        entry["volume"] += score.market.volume_24h
        if not score.structurally_unviable:
            entry["viable"] += 1
            entry["daily"] += score.expected_daily_micros / 1e6

    return {
        "scanned_at_utc": scanned_at_utc,
        "records_scanned": records,
        "combo_markets": combos,
        "real_markets": len(quotes),
        "liquid_markets": len(liquid),
        "viable_markets": len(viable),
        "total_daily_dollars": round(
            sum(s.expected_daily_micros for s in viable) / 1e6, 2
        ),
        "families": sorted(
            (
                {"family": f, **{k: (round(v, 2) if k == "daily" else v) for k, v in a.items()}}
                for f, a in families.items()
            ),
            key=lambda r: -r["daily"],
        ),
        "categories": sorted(
            (
                {"category": c, **{k: (round(v, 2) if k == "daily" else v) for k, v in a.items()}}
                for c, a in categories.items()
            ),
            key=lambda r: -r["daily"],
        ),
        "price_bands": [
            {
                "band": label,
                "markets": bands[label]["markets"],
                "viable": bands[label]["viable"],
                "volume": bands[label]["volume"],
                "viable_volume": bands[label]["viable_volume"],
                "daily": round(bands[label]["daily_micros"] / 1e6, 2),
            }
            for label, _ in PRICE_BANDS
        ],
        "capacity": {
            "all_liquid_volume": sum(q.volume_24h for q in liquid),
            "viable_volume": sum(s.market.volume_24h for s in viable),
            "weighted_net_edge_cents": round(
                sum(s.net_edge_ticks / 100 * s.market.volume_24h for s in viable)
                / max(1, sum(s.market.volume_24h for s in viable)),
                2,
            ),
            "by_share": [
                {"share": s, **totals(BASE_FEE, s)} for s in (0.05, 0.10, 0.25, 0.50, 1.00)
            ],
            "by_schedule": [
                {"label": "Taker 7% on both sides (what we assume)", **totals(BASE_FEE, 0.10)},
                {"label": "Maker $0.0025/contract, taker exit", **totals(MAKER_FEE, 0.10)},
                {"label": "Zero fees (theoretical ceiling)", **totals(NO_FEE, 0.10)},
            ],
        },
        "top_markets": [
            {
                "ticker": s.ticker,
                "mid": round(s.market.mid / ONE_DOLLAR, 2),
                "band": price_band(s.market.mid),
                "spread_cents": round(s.market.spread_ticks / 100, 1),
                "fee_cents": round(s.fee_round_trip_ticks / 100, 2),
                "net_cents": round(s.net_edge_ticks / 100, 2),
                "volume_24h": s.market.volume_24h,
                "daily_dollars": round(s.expected_daily_micros / 1e6, 2),
            }
            for s in viable[:25]
        ],
        # One compact row per liquid market: [mid_ticks, capturable_ticks,
        # contracts_24h]. Small enough to ship, and it lets the opportunity
        # model recompute viability live for any fee schedule or capture
        # assumption instead of baking one scenario in.
        "markets_compact": [
            [q.mid, capturable_ticks(q, DEFAULT_IMPROVEMENT_TICKS), q.volume_24h]
            for q in liquid
        ],
        "notional_per_day": round(
            sum(q.volume_24h * q.mid / ONE_DOLLAR for q in liquid), 2
        ),
        "spread_histogram": dict(
            sorted(collections.Counter(round(q.spread_ticks / 100) for q in liquid).items())
        ),
    }


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit(
            "usage: build_data.py <markets.json> <scanned_at_utc> [records_scanned]"
        )

    path = Path(sys.argv[1])
    raw = json.loads(path.read_text())
    records = int(sys.argv[3]) if len(sys.argv) > 3 else len(raw)

    data = build(raw, scanned_at_utc=sys.argv[2], records=records)
    out = Path(__file__).resolve().parent / "data" / "scan.json"
    out.write_text(json.dumps(data, indent=2))

    print(f"wrote {out}")
    print(
        f"  {data['real_markets']:,} real markets, {data['liquid_markets']} liquid, "
        f"{data['viable_markets']} viable, ${data['total_daily_dollars']:,.2f}/day"
    )
    for band in data["price_bands"]:
        print(
            f"  {band['band']:<30} {band['viable']:>4}/{band['markets']:<4} "
            f"{band['volume']:>9,} contracts  ${band['daily']:>8.2f}/day"
        )


if __name__ == "__main__":
    main()
