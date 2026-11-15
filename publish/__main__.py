"""CLI entrypoint to run statewager across states and write docs/ static assets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import os
import sys
from typing import Sequence

from dateutil.relativedelta import relativedelta

from statewager.model import StateMonthResult, month_starts, parse_month_spec
from statewager.states import ALL_STATES, load_state_module

from .csv_writer import (
    merge_state_results,
    read_state_csv,
    write_combined_csv,
    write_state_csv,
)
from .site import write_index_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m publish",
        description="Fetch sports wagering data via statewager and generate docs/ CSVs + index.html",
    )
    parser.add_argument(
        "--states",
        nargs="+",
        default=list(ALL_STATES),
        help="State slugs to publish (default: all 10 states).",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=13,
        help="Trailing months to fetch when --start-month is omitted (default: 13).",
    )
    parser.add_argument(
        "--start-month",
        help="Earliest month in YYYY-MM (overrides --months; defaults to 2023-01 when --offline is set).",
    )
    parser.add_argument(
        "--end-month",
        help="Latest month in YYYY-MM (defaults to last completed month).",
    )
    parser.add_argument(
        "--cache-dir",
        default="cache",
        help="Source-file cache directory (default: cache).",
    )
    parser.add_argument(
        "--docs-dir",
        default="docs",
        help="Static output directory for GitHub Pages (default: docs).",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Read from --cache-dir only and make zero network requests.",
    )
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="Overwrite existing per-state CSV rows instead of merging by month.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=4,
        help="Parallel worker threads for state fetches (default: 4).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-month statewager progress logs.",
    )
    return parser


def publish_all(
    *,
    states: Sequence[str] = ALL_STATES,
    month_start: str | date,
    month_end: str | date | None = None,
    cache_dir: str | None = "cache",
    docs_dir: str = "docs",
    offline: bool = False,
    quiet: bool = False,
    merge_existing: bool = True,
    jobs: int = 4,
) -> dict[str, list[StateMonthResult]]:
    """Run ``statewager`` for ``states`` and write ``docs/data/*.csv`` + ``docs/index.html``."""
    data_dir = os.path.join(docs_dir, "data")
    by_state: dict[str, list[StateMonthResult]] = {}

    modules = []
    seen_slugs: set[str] = set()
    for raw_state in states:
        mod = load_state_module(raw_state)
        slug = mod.__name__.rsplit(".", 1)[-1]
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        modules.append((slug, mod))

    silent_log = (lambda _msg: None) if quiet else None

    def _fetch_one(item: tuple[str, object]) -> list[StateMonthResult]:
        _slug, mod = item
        return mod.fetch(
            month_start,
            month_end,
            cache_dir=cache_dir,
            offline=offline,
            log=silent_log,
        )

    worker_count = min(max(1, jobs), len(modules)) if modules else 1
    if worker_count > 1:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            fetched_lists = list(pool.map(_fetch_one, modules))
    else:
        fetched_lists = [_fetch_one(item) for item in modules]

    for (slug, mod), results in zip(modules, fetched_lists):
        csv_path = os.path.join(data_dir, f"{slug}.csv")
        if merge_existing:
            merged = merge_state_results(read_state_csv(csv_path), results)
        else:
            merged = sorted(results, key=lambda r: r.month)
        write_state_csv(data_dir, slug, merged)
        by_state[slug] = merged

    combined: list[StateMonthResult] = []
    state_options: list[tuple[str, str]] = []
    if merge_existing:
        for slug in ALL_STATES:
            mod = load_state_module(slug)
            if slug in by_state:
                combined.extend(by_state[slug])
                state_options.append((slug, mod.STATE_NAME))
            else:
                existing_path = os.path.join(data_dir, f"{slug}.csv")
                if os.path.isfile(existing_path):
                    combined.extend(read_state_csv(existing_path))
                    state_options.append((slug, mod.STATE_NAME))
    else:
        for slug, mod in modules:
            combined.extend(by_state[slug])
            state_options.append((slug, mod.STATE_NAME))

    write_combined_csv(data_dir, combined)
    write_index_html(docs_dir, state_options)
    return by_state


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    last_completed = date.today().replace(day=1) - relativedelta(months=1)
    end_date = parse_month_spec(args.end_month) if args.end_month else last_completed
    if args.start_month:
        start_date = parse_month_spec(args.start_month)
    elif args.offline:
        start_date = date(2023, 1, 1)
    else:
        window = month_starts(args.months)
        start_date = window[0]

    by_state = publish_all(
        states=args.states,
        month_start=start_date,
        month_end=end_date,
        cache_dir=args.cache_dir,
        docs_dir=args.docs_dir,
        offline=args.offline,
        quiet=args.quiet,
        merge_existing=not args.no_merge,
        jobs=max(1, args.jobs),
    )
    total_rows = sum(len(rows) for rows in by_state.values())
    populated_states = sum(1 for rows in by_state.values() if rows)
    print(
        f"Published {total_rows} state-month rows across {populated_states}/{len(by_state)} states "
        f"into {args.docs_dir}/ (all.csv + per-state CSVs + index.html)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
