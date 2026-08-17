"""Check whether a running campaign's premises still hold. Read-only.

    python scripts/monitor_campaign.py                    # one check, now
    python scripts/monitor_campaign.py --watch --every 300
    python scripts/monitor_campaign.py --since 2026-08-16T00:00:00Z

Reads the account's own ledger - fills, fees, balance - and reports whether the
facts the strategy depends on are still facts. Places no orders and cancels
nothing: it answers a question, and what to do about the answer is a separate
decision made by a person or by whatever supervises the trader.

Exit codes are the point when this runs from cron or a supervisor:

    0   premises hold
    1   HALT - a premise tripped, or could not be evaluated
    2   could not reach the exchange at all

Note that 1 covers both "conditions turned against us" and "we cannot tell",
deliberately. A monitor that cannot measure its own inputs is not reporting
good news.

The markout tripwire needs the mid at fill time, which the fills endpoint does
not carry. Without a trader recording it, this reports markout as PENDING rather
than inventing a number - so a campaign run purely from this script will halt on
markout after the grace period, which is correct: nobody should be resting size
in a market whose adverse selection is unmeasured.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from kalshi_mm_bot.api.auth import KalshiAuth  # noqa: E402
from kalshi_mm_bot.api.rest import KalshiRestClient  # noqa: E402
from kalshi_mm_bot.config import load_settings  # noqa: E402
from kalshi_mm_bot.live.campaign import (  # noqa: E402
    CampaignLimits,
    CampaignMonitor,
    CampaignSample,
    fills_from_ledger,
)
from kalshi_mm_bot.market.price import MONEY_SCALE  # noqa: E402

EXIT_OK = 0
EXIT_HALT = 1
EXIT_UNREACHABLE = 2


def log(message: str) -> None:
    print(f"[{datetime.now(UTC).strftime('%H:%M:%S')}] {message}", flush=True)


async def fetch_fills(rest: KalshiRestClient, since_ts: int | None) -> list[dict]:
    """Every fill, newest first, stopping once we pass the window."""

    collected: list[dict] = []
    cursor = None

    for _ in range(20):
        params: dict = {"limit": 200}

        if cursor:
            params["cursor"] = cursor

        data = await rest._request("GET", "/portfolio/fills", params=params)
        page = data.get("fills") or []
        collected.extend(page)
        cursor = data.get("cursor")

        if not cursor or not page:
            break

        if since_ts is not None and page and int(page[-1].get("ts") or 0) < since_ts:
            break

    if since_ts is not None:
        collected = [f for f in collected if int(f.get("ts") or 0) >= since_ts]

    return collected


def load_mids(path: Path | None) -> dict[str, int]:
    """Optional map of fill_id -> mid at fill, written by the trader.

    Absent by default, which is why markout reports PENDING rather than zero.
    """

    if path is None or not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        log(f"could not read mids from {path}: {error}")
        return {}

    return {str(k): int(v) for k, v in payload.items()} if isinstance(payload, dict) else {}


async def check_once(args: argparse.Namespace, monitor: CampaignMonitor) -> int:
    settings = load_settings()
    environment = settings.environment(prod=True)
    rest = KalshiRestClient(
        environment.rest_base_url,
        KalshiAuth(settings.api_key_id, settings.private_key_path),
    )

    try:
        try:
            balance_micros = int(await rest.get_available_balance_cents()) * (
                MONEY_SCALE // 100
            )
            raw_fills = await fetch_fills(rest, args.since_ts)
        except Exception as error:
            # Cannot reach the exchange. Distinct from a tripped premise, and
            # still not good news - a supervisor should treat it as "stop".
            log(f"UNREACHABLE: {type(error).__name__}: {error}")
            return EXIT_UNREACHABLE

        fills = fills_from_ledger(raw_fills, mids=load_mids(args.mids))
        sample = CampaignSample(
            fills=fills,
            balance_micros=balance_micros,
            realized_pnl_micros=None,
            elapsed_seconds=args.elapsed,
            quotes_placed=args.quotes_placed,
        )
        verdict = monitor.assess(sample)

        log(
            f"{len(fills)} fill(s) in window, balance "
            f"${balance_micros / MONEY_SCALE:,.2f}"
        )
        print(verdict.describe())

        return EXIT_HALT if verdict.should_halt else EXIT_OK
    finally:
        await rest.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        help="ISO timestamp; only fills at or after this count. Default: last 24h.",
    )
    parser.add_argument("--watch", action="store_true", help="Keep checking.")
    parser.add_argument("--every", type=float, default=300.0)
    parser.add_argument(
        "--mids",
        type=Path,
        help="JSON map of fill_id -> mid at fill, for the markout tripwire.",
    )
    parser.add_argument(
        "--quotes-placed",
        type=int,
        default=0,
        help="Quotes placed in the window, for the fill-rate tripwire.",
    )
    parser.add_argument(
        "--elapsed",
        type=float,
        default=0.0,
        help="Seconds the campaign has been running. Drives whether an "
        "unmeasurable premise is PENDING or a halt.",
    )
    parser.add_argument("--min-balance", type=float, help="Halt below this balance.")
    parser.add_argument("--max-loss", type=float, help="Halt past this session loss.")
    args = parser.parse_args()

    if args.since:
        try:
            args.since_ts = int(
                datetime.fromisoformat(args.since.replace("Z", "+00:00")).timestamp()
            )
        except ValueError:
            raise SystemExit(f"could not parse --since {args.since!r}")
    else:
        args.since_ts = int(time.time()) - 86_400

    limits = CampaignLimits(
        min_balance_micros=(
            int(args.min_balance * MONEY_SCALE) if args.min_balance else None
        ),
        max_session_loss_micros=(
            int(args.max_loss * MONEY_SCALE) if args.max_loss else None
        ),
    )
    monitor = CampaignMonitor(limits=limits)
    started = time.monotonic()

    while True:
        if not args.elapsed:
            args.elapsed = time.monotonic() - started

        code = asyncio.run(check_once(args, monitor))

        if not args.watch:
            raise SystemExit(code)

        if code == EXIT_HALT:
            # Latched. Continuing to poll after a halt would only produce a
            # reading that looks fine and invite someone to ignore the halt.
            log("halted - stopping the watch. Investigate before resuming.")
            raise SystemExit(code)

        time.sleep(args.every)
        args.elapsed = time.monotonic() - started


if __name__ == "__main__":
    main()
