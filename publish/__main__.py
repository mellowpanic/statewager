"""CLI entrypoint to run statewager across states and write docs/ static assets."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
import logging
import os
import sys
import tempfile
from types import ModuleType
from typing import Sequence

from dateutil.relativedelta import relativedelta

from statewager.model import StateMonthResult, parse_month_spec, resolve_fetch_months
from statewager.states import ALL_STATES, load_state_module

from .csv_writer import (
    PublishedDetailRow,
    extract_published_details,
    merge_detail_results,
    merge_state_results,
    read_detail_csv,
    read_state_csv,
    write_combined_csv,
    write_combined_detail_csv,
    write_state_csv,
    write_state_detail_csv,
    write_status_json,
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
        help=f"State slugs to publish (default: all {len(ALL_STATES)} states).",
    )
    parser.add_argument(
        "--months",
        type=int,
        default=13,
        help="Trailing months to fetch when --start-month is omitted (default: 13).",
    )
    parser.add_argument(
        "--recheck-months",
        type=int,
        default=None,
        help="When merging, re-fetch only the latest N trailing months plus any missing months in the window.",
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
    recheck_months: int | None = None,
) -> dict[str, list[StateMonthResult]]:
    """Run ``statewager`` for ``states`` and write ``docs/data/*.csv`` + ``status.json`` + ``index.html``."""
    cache_dir = (cache_dir or "").strip() or None
    data_dir = os.path.join(docs_dir, "data")
    details_dir = os.path.join(data_dir, "details")
    by_state: dict[str, list[StateMonthResult]] = {}
    details_by_state: dict[str, list[PublishedDetailRow]] = {}
    pub_log = logging.getLogger("publish")

    modules: list[tuple[str, ModuleType]] = []
    loaded_modules: dict[str, ModuleType] = {}
    for raw_state in states:
        mod = load_state_module(raw_state)
        slug = mod.__name__.rsplit(".", 1)[-1]
        if slug in loaded_modules:
            continue
        loaded_modules[slug] = mod
        modules.append((slug, mod))
    if not modules:
        raise ValueError("No states selected")

    target_dates_by_slug = {
        slug: resolve_fetch_months(month_start, month_end, launch_date=getattr(mod, "LAUNCH_DATE", None))
        for slug, mod in modules
    }
    expected_by_slug = {slug: bool(dates) for slug, dates in target_dates_by_slug.items()}
    fetch_bounds_by_slug: dict[str, tuple[str | date, str | date | None]] = {
        slug: (month_start, month_end) for slug, _ in modules
    }
    existing_state_cache: dict[str, list[StateMonthResult]] = {}
    if merge_existing and recheck_months is not None and recheck_months >= 1:
        for slug, _mod in modules:
            target_dates = target_dates_by_slug[slug]
            if not target_dates:
                continue
            cached_rows = read_state_csv(os.path.join(data_dir, f"{slug}.csv"))
            existing_state_cache[slug] = cached_rows
            existing_months = {r.month for r in cached_rows}
            needed = sorted(
                set(target_dates[-recheck_months:])
                | {d for d in target_dates if d.strftime("%Y-%m") not in existing_months}
            )
            if needed:
                fetch_bounds_by_slug[slug] = (needed[0], needed[-1])
    silent_log = (lambda _msg: None) if quiet else None

    def _fetch_one(item: tuple[str, ModuleType]) -> tuple[list[StateMonthResult], Exception | None]:
        slug, mod = item
        f_start, f_end = fetch_bounds_by_slug[slug]
        try:
            return (
                mod.fetch(
                    f_start,
                    f_end,
                    cache_dir=cache_dir,
                    offline=offline,
                    log=silent_log,
                ),
                None,
            )
        except Exception as exc:
            if not merge_existing:
                raise
            pub_log.warning("%s: refresh failed (%r); keeping existing rows", slug, exc)
            return ([], exc)

    worker_count = min(max(1, jobs), len(modules))
    if worker_count > 1:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            fetched_lists = list(pool.map(_fetch_one, modules))
    else:
        fetched_lists = [_fetch_one(item) for item in modules]

    if all(exc is not None for _res, exc in fetched_lists):
        first_exc = fetched_lists[0][1]
        raise RuntimeError(f"All {len(modules)} state refreshes failed") from first_exc
    if any(expected_by_slug.values()) and all(not res for res, _exc in fetched_lists):
        raise RuntimeError(f"All {len(modules)} state refreshes returned 0 rows")

    for (slug, mod), (results, exc) in zip(modules, fetched_lists):
        csv_path = os.path.join(data_dir, f"{slug}.csv")
        detail_csv_path = os.path.join(details_dir, f"{slug}.csv")
        has_details = bool(getattr(mod, "HAS_DETAILS", False))
        existing_rows = existing_state_cache.get(slug)
        keep_existing = exc is not None or (
            not merge_existing and not results and expected_by_slug[slug] and os.path.isfile(csv_path)
        )
        if keep_existing:
            if exc is None:
                pub_log.warning("%s: refresh returned 0 rows under --no-merge; keeping existing rows", slug)
            by_state[slug] = existing_rows if existing_rows is not None else read_state_csv(csv_path)
            if has_details:
                details_by_state[slug] = read_detail_csv(detail_csv_path)
            continue

        if merge_existing:
            prev_rows = existing_rows if existing_rows is not None else read_state_csv(csv_path)
            merged = merge_state_results(prev_rows, results)
        else:
            merged = sorted(results, key=lambda r: r.month)
        write_state_csv(data_dir, slug, merged)
        by_state[slug] = merged

        if has_details:
            detail_source_results = results
            if merge_existing:
                merged_urls = {(r.state, r.month): r.source_url for r in merged if r.source_url is not None}
                detail_source_results = [
                    replace(r, source_url=merged_urls[(r.state, r.month)])
                    if r.source_url is None and (r.state, r.month) in merged_urls
                    else r
                    for r in results
                ]
            fresh_details = extract_published_details(detail_source_results)
            if merge_existing:
                merged_details = merge_detail_results(
                    read_detail_csv(detail_csv_path),
                    fresh_details,
                    refreshed_months={(r.state, r.month) for r in results},
                )
            else:
                merged_details = sorted(fresh_details, key=lambda r: (r.month, r.operator))
            write_state_detail_csv(details_dir, slug, merged_details)
            details_by_state[slug] = merged_details

    combined: list[StateMonthResult] = []
    combined_details: list[PublishedDetailRow] = []
    state_options: list[tuple[str, str]] = []
    for slug in ALL_STATES:
        mod = loaded_modules.get(slug) or load_state_module(slug)
        existing_path = os.path.join(data_dir, f"{slug}.csv")
        if slug in by_state:
            rows = by_state[slug]
            combined.extend(rows)
            if rows or os.path.isfile(existing_path):
                state_options.append((slug, mod.STATE_NAME))
        elif os.path.isfile(existing_path):
            combined.extend(read_state_csv(existing_path))
            state_options.append((slug, mod.STATE_NAME))

        if getattr(mod, "HAS_DETAILS", False):
            if slug in details_by_state:
                combined_details.extend(details_by_state[slug])
            else:
                existing_detail_path = os.path.join(details_dir, f"{slug}.csv")
                if os.path.isfile(existing_detail_path):
                    combined_details.extend(read_detail_csv(existing_detail_path))

    write_combined_csv(data_dir, combined)
    write_combined_detail_csv(details_dir, combined_details)
    write_status_json(data_dir, combined, combined_details, state_options)
    write_index_html(docs_dir, state_options)
    return by_state


def _verify_docs_dir_writable(docs_dir: str) -> None:
    os.makedirs(docs_dir, exist_ok=True)
    with tempfile.TemporaryFile(dir=docs_dir):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.months < 1:
        parser.error("--months must be >= 1")
    if args.recheck_months is not None and args.recheck_months < 1:
        parser.error("--recheck-months must be >= 1")
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")

    for raw_state in args.states:
        try:
            load_state_module(raw_state)
        except ValueError as exc:
            parser.error(str(exc))

    current_month = date.today().replace(day=1)
    last_completed = current_month - relativedelta(months=1)
    try:
        end_date = parse_month_spec(args.end_month) if args.end_month else last_completed
        if args.start_month:
            start_date = parse_month_spec(args.start_month)
            if not args.end_month and start_date > end_date:
                end_date = start_date
        elif args.offline:
            start_date = date(2023, 1, 1)
        else:
            start_date = end_date - relativedelta(months=args.months - 1)
    except ValueError as exc:
        parser.error(str(exc))
    if end_date < start_date:
        parser.error(f"--end-month ({end_date:%Y-%m}) must be on or after --start-month ({start_date:%Y-%m})")
    if end_date > current_month:
        parser.error(f"--end-month ({end_date:%Y-%m}) cannot be in the future")

    try:
        _verify_docs_dir_writable(args.docs_dir)
    except OSError as exc:
        parser.error(f"--docs-dir {args.docs_dir!r} is not writable: {exc}")

    by_state = publish_all(
        states=args.states,
        month_start=start_date,
        month_end=end_date,
        cache_dir=args.cache_dir,
        docs_dir=args.docs_dir,
        offline=args.offline,
        quiet=args.quiet,
        merge_existing=not args.no_merge,
        jobs=args.jobs,
        recheck_months=args.recheck_months,
    )
    total_rows = sum(len(rows) for rows in by_state.values())
    populated_states = sum(1 for rows in by_state.values() if rows)
    print(
        f"Published {total_rows} state-month rows across {populated_states}/{len(by_state)} states "
        f"into {args.docs_dir}/ (all.csv + per-state CSVs + status.json + index.html)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
