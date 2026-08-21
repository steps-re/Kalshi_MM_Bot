"""Pull corpus files out of the cold archive, on demand.

    python scripts/fetch_corpus.py candles.jsonl candles_tennis.jsonl
    python scripts/fetch_corpus.py --list
    python scripts/fetch_corpus.py --all-candles

The Kalshi corpus is ~33GB and no longer lives on the laptop. It was archived
to GCS on 2026-08-20, verified byte-exact (853 files / 32,997,666,273 bytes)
before deletion, and every script that used to read `~/kalshi-audit/...`
directly now needs a restore step first.

This fetches only the files a given run needs into a local cache and skips
anything already there, so re-running a study does not re-download 33GB. Delete
the cache whenever you like; it is a cache.

**Account matters.** The archive lives in `forge-steps-ventures` and resolves
for `mike@stepsventures.com`. The Airloom account does not see it, and a 403
here reads exactly like "the data is gone" - which has already cost this
project one wrongly-written audit section.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

BUCKET = "gs://steps-cold-archive-2026/kalshi-audit"
ACCOUNT = "mike@stepsventures.com"
PROJECT = "forge-steps-ventures"
CACHE = Path.home() / "kalshi-audit"

# The files the calibration and tennis studies actually read. Named here so a
# study can ask for its inputs by role rather than by remembering filenames.
BUNDLES = {
    "candles": ("candles.jsonl", "candles_breadth.jsonl",
                "candles_tennis.jsonl", "candles_other.jsonl"),
    "settled": ("settled_compact.jsonl.gz", "settled_history.jsonl.gz"),
    "slices": tuple(f"settled_slices/settled_to_2026-{d}.jsonl.gz" for d in
                    ("07-14", "07-19", "07-24", "07-29", "08-03", "08-08")),
}


def run(args: list[str], capture: bool = False):
    return subprocess.run(args, check=False, text=True,
                          capture_output=capture)


def gcs(args: list[str], capture: bool = False):
    return run(["gcloud", "storage", *args,
                f"--account={ACCOUNT}", f"--project={PROJECT}"], capture)


def listing() -> None:
    result = gcs(["ls", "-l", f"{BUCKET}/"], capture=True)

    if result.returncode:
        raise SystemExit(f"could not list the archive:\n{result.stderr}")

    print(result.stdout)


def fetch(names: list[str], cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    wanted, present = [], []

    for name in names:
        if (cache / name).exists():
            present.append(name)
        else:
            wanted.append(name)

    for name in present:
        size = (cache / name).stat().st_size / 1e6
        print(f"  cached  {name} ({size:,.0f} MB)")

    if not wanted:
        print(f"\nall {len(names)} file(s) already in {cache}")
        return

    for name in wanted:
        target = cache / name
        target.parent.mkdir(parents=True, exist_ok=True)
        print(f"  fetch   {name} ...", flush=True)
        result = gcs(["cp", f"{BUCKET}/{name}", str(target)])

        if result.returncode:
            raise SystemExit(
                f"\nfailed to fetch {name}. If this is a 403 rather than a "
                f"404, the corpus is NOT missing - you are on the wrong "
                f"account. This needs {ACCOUNT} on {PROJECT}.")

    total = sum((cache / n).stat().st_size for n in names) / 1e6
    print(f"\n{len(names)} file(s) ready in {cache} ({total:,.0f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="file names inside the archive")
    parser.add_argument("--list", action="store_true",
                        help="show what is in the archive")
    parser.add_argument("--bundle", choices=sorted(BUNDLES),
                        action="append", default=[],
                        help="fetch a named group of files")
    parser.add_argument("--all-candles", action="store_true",
                        help="shorthand for --bundle candles")
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args()

    if args.list:
        listing()
        return

    names = list(args.files)

    for bundle in args.bundle:
        names.extend(BUNDLES[bundle])

    if args.all_candles:
        names.extend(BUNDLES["candles"])

    names = list(dict.fromkeys(names))

    if not names:
        parser.error("name at least one file, a --bundle, or use --list")

    print(f"archive: {BUCKET}\ncache:   {args.cache}\n")
    fetch(names, args.cache)


if __name__ == "__main__":
    main()
