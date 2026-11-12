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
