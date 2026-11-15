"""CSV serialization helpers for StateMonthResult datasets."""
from __future__ import annotations

import csv
import os
from typing import Iterable, Sequence

from statewager.model import CORE_RESULT_FIELDS, StateMonthResult

CSV_HEADERS: list[str] = list(CORE_RESULT_FIELDS)


def _format_amount(value: float | None) -> str:
    if value is None:
        return ""
    rounded_int = round(value)
    if abs(value - rounded_int) < 0.005:
        return str(int(rounded_int))
    return f"{value:.2f}"


def _format_pct(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}"


def _parse_opt_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    return float(text)


def read_state_csv(csv_path: str) -> list[StateMonthResult]:
    """Load previously published rows from a per-state CSV if it exists."""
    if not os.path.isfile(csv_path):
        return []
    rows: list[StateMonthResult] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            state = (raw.get("state") or "").strip()
            month = (raw.get("month") or "").strip()
            if not state or not month:
                continue
            rows.append(
                StateMonthResult(
                    state=state,
                    month=month,
                    handle=_parse_opt_float(raw.get("handle")) or 0.0,
                    revenue=_parse_opt_float(raw.get("revenue")) or 0.0,
                    hold=_parse_opt_float(raw.get("hold")) or 0.0,
                    retail_handle=_parse_opt_float(raw.get("retail_handle")),
                    retail_revenue=_parse_opt_float(raw.get("retail_revenue")),
                    retail_hold=_parse_opt_float(raw.get("retail_hold")),
                    mobile_handle=_parse_opt_float(raw.get("mobile_handle")),
                    mobile_revenue=_parse_opt_float(raw.get("mobile_revenue")),
                    mobile_hold=_parse_opt_float(raw.get("mobile_hold")),
                    source_url=(raw.get("source_url") or "").strip() or None,
                )
            )
    return rows


def merge_state_results(
    existing: Iterable[StateMonthResult],
    fresh: Iterable[StateMonthResult],
) -> list[StateMonthResult]:
    """Merge ``fresh`` results onto ``existing`` rows keyed by ``(state, month)``."""
    by_key: dict[tuple[str, str], StateMonthResult] = {}
    for item in existing:
        by_key[(item.state, item.month)] = item
    for item in fresh:
        by_key[(item.state, item.month)] = item
    return sorted(by_key.values(), key=lambda r: r.month)


def result_to_csv_row(item: StateMonthResult) -> list[str]:
    """Serialize a ``StateMonthResult`` into the 12-column CSV row schema."""
    return [
        item.state,
        item.month,
        _format_amount(item.handle),
        _format_amount(item.revenue),
        _format_pct(item.hold),
        _format_amount(item.retail_handle),
        _format_amount(item.retail_revenue),
        _format_pct(item.retail_hold),
        _format_amount(item.mobile_handle),
        _format_amount(item.mobile_revenue),
        _format_pct(item.mobile_hold),
        item.source_url or "",
    ]


def write_state_csv(
    data_dir: str,
    state_slug: str,
    results: Sequence[StateMonthResult],
) -> str:
    """Write ``<data_dir>/<state_slug>.csv`` sorted by ISO month and return its path."""
    os.makedirs(data_dir, exist_ok=True)
    ordered = sorted(results, key=lambda r: r.month)
    out_path = os.path.join(data_dir, f"{state_slug}.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADERS)
        for item in ordered:
            writer.writerow(result_to_csv_row(item))
    return out_path


def write_combined_csv(
    data_dir: str,
    all_results: Iterable[StateMonthResult],
) -> str:
    """Write ``<data_dir>/all.csv`` sorted by ``(state, month)`` and return its path."""
    os.makedirs(data_dir, exist_ok=True)
    ordered = sorted(all_results, key=lambda r: (r.state, r.month))
    out_path = os.path.join(data_dir, "all.csv")
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADERS)
        for item in ordered:
            writer.writerow(result_to_csv_row(item))
    return out_path
