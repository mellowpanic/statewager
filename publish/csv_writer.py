"""CSV serialization helpers for StateMonthResult datasets."""
from __future__ import annotations

import csv
import math
import os
import tempfile
from typing import Callable, Iterable, Sequence, TextIO

from statewager.model import CORE_RESULT_FIELDS, StateMonthResult, safe_float

CSV_HEADERS: list[str] = list(CORE_RESULT_FIELDS)


def _format_amount(value: float | None) -> str:
    num = safe_float(value)
    if num is None:
        return ""
    rounded_int = round(num)
    if abs(num - rounded_int) < 0.005:
        return str(int(rounded_int))
    return f"{num:.2f}"


def _format_pct(value: float | None) -> str:
    num = safe_float(value)
    if num is None:
        return ""
    if abs(num) < 0.005:
        num = 0.0
    return f"{num:.2f}"


def _parse_opt_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    val = float(text)
    if not math.isfinite(val):
        return None
    return 0.0 if val == 0.0 else val


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
                    handle=_parse_opt_float(raw.get("handle")),
                    revenue=_parse_opt_float(raw.get("revenue")),
                    hold=_parse_opt_float(raw.get("hold")),
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
    return sorted(by_key.values(), key=lambda r: (r.month, r.state))


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


def atomic_write_file(
    out_path: str,
    write_fn: Callable[[TextIO], None],
    *,
    suffix: str = ".tmp",
    newline: str | None = None,
) -> str:
    """Atomically write ``out_path`` via a temporary file with 0644 permissions."""
    parent = os.path.dirname(os.path.abspath(out_path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=suffix, dir=parent)
    try:
        with os.fdopen(fd, "w", newline=newline, encoding="utf-8") as fh:
            write_fn(fh)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, out_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    return out_path


def _atomic_write_csv(out_path: str, ordered: Sequence[StateMonthResult]) -> str:
    def _write(fh: TextIO) -> None:
        writer = csv.writer(fh)
        writer.writerow(CSV_HEADERS)
        for item in ordered:
            writer.writerow(result_to_csv_row(item))

    return atomic_write_file(out_path, _write, suffix=".csv", newline="")


def write_state_csv(
    data_dir: str,
    state_slug: str,
    results: Sequence[StateMonthResult],
) -> str:
    """Write ``<data_dir>/<state_slug>.csv`` sorted by ISO month and return its path."""
    ordered = sorted(results, key=lambda r: r.month)
    out_path = os.path.join(data_dir, f"{state_slug}.csv")
    return _atomic_write_csv(out_path, ordered)


def write_combined_csv(
    data_dir: str,
    all_results: Iterable[StateMonthResult],
) -> str:
    """Write ``<data_dir>/all.csv`` sorted by ``(state, month)`` and return its path."""
    ordered = sorted(all_results, key=lambda r: (r.state, r.month))
    out_path = os.path.join(data_dir, "all.csv")
    return _atomic_write_csv(out_path, ordered)
