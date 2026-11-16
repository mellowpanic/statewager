"""CSV serialization helpers for StateMonthResult datasets."""
from __future__ import annotations

from dataclasses import dataclass
import csv
import math
import os
import tempfile
from typing import Callable, Iterable, Mapping, Sequence, TextIO

from statewager.model import CORE_RESULT_FIELDS, StateMonthResult, hold_pct, safe_float
from statewager.operators import classify_operator_brand, normalize_operator_name

CSV_HEADERS: list[str] = [
    *CORE_RESULT_FIELDS,
    "gross_revenue",
    "promo_deductions",
    "tax",
]
DETAIL_CSV_HEADERS: list[str] = [
    "state",
    "month",
    "operator",
    "brand",
    "handle",
    "revenue",
    "hold",
    "retail_handle",
    "retail_revenue",
    "retail_hold",
    "mobile_handle",
    "mobile_revenue",
    "mobile_hold",
    "source_url",
    "gross_revenue",
    "promo_deductions",
    "tax",
]


@dataclass(slots=True)
class PublishedDetailRow:
    """Normalized operator/venue-level monthly regulatory row for public CSV export."""

    state: str
    month: str
    operator: str
    brand: str
    handle: float | None
    revenue: float | None
    hold: float | None
    retail_handle: float | None = None
    retail_revenue: float | None = None
    retail_hold: float | None = None
    mobile_handle: float | None = None
    mobile_revenue: float | None = None
    mobile_hold: float | None = None
    source_url: str | None = None
    gross_revenue: float | None = None
    promo_deductions: float | None = None
    tax: float | None = None


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
    try:
        val = float(text)
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    return 0.0 if val == 0.0 else val


def _parse_csv_metric_fields(raw: Mapping[str, str | None]) -> dict[str, float | str | None]:
    return {
        "handle": _parse_opt_float(raw.get("handle")),
        "revenue": _parse_opt_float(raw.get("revenue")),
        "hold": _parse_opt_float(raw.get("hold")),
        "retail_handle": _parse_opt_float(raw.get("retail_handle")),
        "retail_revenue": _parse_opt_float(raw.get("retail_revenue")),
        "retail_hold": _parse_opt_float(raw.get("retail_hold")),
        "mobile_handle": _parse_opt_float(raw.get("mobile_handle")),
        "mobile_revenue": _parse_opt_float(raw.get("mobile_revenue")),
        "mobile_hold": _parse_opt_float(raw.get("mobile_hold")),
        "source_url": (raw.get("source_url") or "").strip() or None,
        "gross_revenue": _parse_opt_float(raw.get("gross_revenue")),
        "promo_deductions": _parse_opt_float(raw.get("promo_deductions")),
        "tax": _parse_opt_float(raw.get("tax")),
    }


def _format_csv_metric_cells(item: StateMonthResult | PublishedDetailRow) -> list[str]:
    return [
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
        _format_amount(item.gross_revenue),
        _format_amount(item.promo_deductions),
        _format_amount(item.tax),
    ]


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
            rows.append(StateMonthResult(state=state, month=month, **_parse_csv_metric_fields(raw)))  # type: ignore[arg-type]
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
    """Serialize a ``StateMonthResult`` into the 15-column CSV row schema."""
    return [item.state, item.month, *_format_csv_metric_cells(item)]


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


def _atomic_write_csv(
    out_path: str,
    headers: Sequence[str],
    row_iterable: Iterable[Sequence[str]],
) -> str:
    def _write(fh: TextIO) -> None:
        writer = csv.writer(fh)
        writer.writerow(headers)
        for row in row_iterable:
            writer.writerow(row)

    return atomic_write_file(out_path, _write, suffix=".csv", newline="")


def _sum_opt(current: float | None, added: float | None) -> float | None:
    if added is None:
        return current
    return added if current is None else current + added


@dataclass(slots=True)
class _DetailAccumulator:
    brand: str
    handle: float | None
    revenue: float | None
    retail_handle: float | None
    retail_revenue: float | None
    mobile_handle: float | None
    mobile_revenue: float | None
    source_url: str | None
    gross_revenue: float | None
    promo_deductions: float | None
    tax: float | None


def _make_detail_row(
    *,
    state: str,
    month: str,
    operator: str,
    acc: _DetailAccumulator,
) -> PublishedDetailRow:
    return PublishedDetailRow(
        state=state,
        month=month,
        operator=operator,
        brand=acc.brand,
        handle=acc.handle,
        revenue=acc.revenue,
        hold=hold_pct(acc.handle, acc.revenue),
        retail_handle=acc.retail_handle,
        retail_revenue=acc.retail_revenue,
        retail_hold=hold_pct(acc.retail_handle, acc.retail_revenue),
        mobile_handle=acc.mobile_handle,
        mobile_revenue=acc.mobile_revenue,
        mobile_hold=hold_pct(acc.mobile_handle, acc.mobile_revenue),
        source_url=acc.source_url,
        gross_revenue=acc.gross_revenue,
        promo_deductions=acc.promo_deductions,
        tax=acc.tax,
    )


def extract_published_details(results: Iterable[StateMonthResult]) -> list[PublishedDetailRow]:
    """Normalize and aggregate operator/venue details across ``StateMonthResult`` records."""
    grouped: dict[tuple[str, str, str], _DetailAccumulator] = {}
    for item in results:
        source_details = item.publish_details if item.publish_details else item.details
        for op in source_details:
            display_name = normalize_operator_name(op.operator, state=item.state)
            if not display_name:
                continue
            brand = classify_operator_brand(op.operator, state=item.state)
            key = (item.state, item.month, display_name)
            acc = grouped.get(key)
            if acc is None:
                grouped[key] = _DetailAccumulator(
                    brand=brand,
                    handle=safe_float(op.handle),
                    revenue=safe_float(op.revenue),
                    retail_handle=safe_float(op.retail_handle),
                    retail_revenue=safe_float(op.retail_revenue),
                    mobile_handle=safe_float(op.mobile_handle),
                    mobile_revenue=safe_float(op.mobile_revenue),
                    source_url=item.source_url,
                    gross_revenue=safe_float(op.gross_revenue),
                    promo_deductions=safe_float(op.promo_deductions),
                    tax=safe_float(op.tax),
                )
            else:
                if acc.brand == "Other / Retail Venue" and brand != "Other / Retail Venue":
                    acc.brand = brand
                acc.handle = _sum_opt(acc.handle, safe_float(op.handle))
                acc.revenue = _sum_opt(acc.revenue, safe_float(op.revenue))
                acc.retail_handle = _sum_opt(acc.retail_handle, safe_float(op.retail_handle))
                acc.retail_revenue = _sum_opt(acc.retail_revenue, safe_float(op.retail_revenue))
                acc.mobile_handle = _sum_opt(acc.mobile_handle, safe_float(op.mobile_handle))
                acc.mobile_revenue = _sum_opt(acc.mobile_revenue, safe_float(op.mobile_revenue))
                acc.gross_revenue = _sum_opt(acc.gross_revenue, safe_float(op.gross_revenue))
                acc.promo_deductions = _sum_opt(acc.promo_deductions, safe_float(op.promo_deductions))
                acc.tax = _sum_opt(acc.tax, safe_float(op.tax))

    rows = [
        _make_detail_row(state=state, month=month, operator=operator, acc=acc)
        for (state, month, operator), acc in grouped.items()
    ]
    return sorted(rows, key=lambda r: (r.state, r.month, r.operator))


def read_detail_csv(csv_path: str) -> list[PublishedDetailRow]:
    """Load previously published detail rows from a per-state detail CSV if it exists."""
    if not os.path.isfile(csv_path):
        return []
    rows: list[PublishedDetailRow] = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            state = (raw.get("state") or "").strip()
            month = (raw.get("month") or "").strip()
            raw_operator = (raw.get("operator") or "").strip()
            if not state or not month or not raw_operator:
                continue
            operator = normalize_operator_name(raw_operator, state=state)
            brand = (raw.get("brand") or "").strip() or classify_operator_brand(operator, state=state)
            rows.append(
                PublishedDetailRow(
                    state=state,
                    month=month,
                    operator=operator,
                    brand=brand,
                    **_parse_csv_metric_fields(raw),  # type: ignore[arg-type]
                )
            )
    return rows


def merge_detail_results(
    existing: Iterable[PublishedDetailRow],
    fresh: Iterable[PublishedDetailRow],
) -> list[PublishedDetailRow]:
    """Merge ``fresh`` detail rows onto ``existing``, replacing each refreshed ``(state, month)``."""
    fresh_list = list(fresh)
    replaced_months = {(row.state, row.month) for row in fresh_list}
    kept = [row for row in existing if (row.state, row.month) not in replaced_months]
    return sorted(kept + fresh_list, key=lambda r: (r.state, r.month, r.operator))


def detail_to_csv_row(item: PublishedDetailRow) -> list[str]:
    """Serialize a ``PublishedDetailRow`` into the 17-column detail CSV row schema."""
    return [item.state, item.month, item.operator, item.brand, *_format_csv_metric_cells(item)]


def write_state_csv(
    data_dir: str,
    state_slug: str,
    results: Sequence[StateMonthResult],
) -> str:
    """Write ``<data_dir>/<state_slug>.csv`` sorted by ISO month and return its path."""
    ordered = sorted(results, key=lambda r: r.month)
    out_path = os.path.join(data_dir, f"{state_slug}.csv")
    return _atomic_write_csv(out_path, CSV_HEADERS, (result_to_csv_row(item) for item in ordered))


def write_combined_csv(
    data_dir: str,
    all_results: Iterable[StateMonthResult],
) -> str:
    """Write ``<data_dir>/all.csv`` sorted by ``(state, month)`` and return its path."""
    ordered = sorted(all_results, key=lambda r: (r.state, r.month))
    out_path = os.path.join(data_dir, "all.csv")
    return _atomic_write_csv(out_path, CSV_HEADERS, (result_to_csv_row(item) for item in ordered))


def write_state_detail_csv(
    details_dir: str,
    state_slug: str,
    rows: Sequence[PublishedDetailRow],
) -> str:
    """Write ``<details_dir>/<state_slug>.csv`` sorted by ``(month, operator)``."""
    ordered = sorted(rows, key=lambda r: (r.month, r.operator))
    out_path = os.path.join(details_dir, f"{state_slug}.csv")
    return _atomic_write_csv(out_path, DETAIL_CSV_HEADERS, (detail_to_csv_row(item) for item in ordered))


def write_combined_detail_csv(
    details_dir: str,
    all_rows: Iterable[PublishedDetailRow],
) -> str:
    """Write ``<details_dir>/all.csv`` sorted by ``(state, month, operator)``."""
    ordered = sorted(all_rows, key=lambda r: (r.state, r.month, r.operator))
    out_path = os.path.join(details_dir, "all.csv")
    return _atomic_write_csv(out_path, DETAIL_CSV_HEADERS, (detail_to_csv_row(item) for item in ordered))
