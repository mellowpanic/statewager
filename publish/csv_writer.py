"""CSV serialization helpers for StateMonthResult datasets."""
from __future__ import annotations

from dataclasses import dataclass, replace
import csv
import os
import re
import tempfile
from typing import Callable, Iterable, Mapping, Sequence, TextIO

from statewager.model import _DC_SLOTS, CORE_RESULT_FIELDS, StateMonthResult, hold_pct, safe_float, sum_optional
from statewager.operators import classify_operator_brand, normalize_operator_name

CSV_HEADERS: list[str] = [
    *CORE_RESULT_FIELDS,
    "gross_revenue",
    "promo_deductions",
    "tax",
]
DETAIL_CSV_HEADERS: list[str] = [
    *CORE_RESULT_FIELDS[:2],
    "operator",
    "brand",
    *CORE_RESULT_FIELDS[2:],
    "gross_revenue",
    "promo_deductions",
    "tax",
]


@dataclass(**_DC_SLOTS)
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
    num = round(num, 2)
    if num == 0.0:
        return "0"
    rounded_int = round(num)
    if num == rounded_int:
        return str(int(rounded_int))
    return f"{num:.2f}"


_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _clean_csv_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _CTRL_RE.sub("", value).strip()
    return cleaned or None


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
        return safe_float(float(text))
    except ValueError:
        return None


_NUMERIC_METRIC_FIELDS: tuple[str, ...] = (
    "handle",
    "revenue",
    "hold",
    "retail_handle",
    "retail_revenue",
    "retail_hold",
    "mobile_handle",
    "mobile_revenue",
    "mobile_hold",
    "gross_revenue",
    "promo_deductions",
    "tax",
)
_ACC_SUM_FIELDS: tuple[str, ...] = (
    "handle",
    "revenue",
    "retail_handle",
    "retail_revenue",
    "mobile_handle",
    "mobile_revenue",
    "gross_revenue",
    "promo_deductions",
    "tax",
)


def _parse_csv_metric_fields(raw: Mapping[str, str | None]) -> dict[str, float | str | None]:
    parsed: dict[str, float | str | None] = {
        field: _parse_opt_float(raw.get(field)) for field in _NUMERIC_METRIC_FIELDS
    }
    parsed["source_url"] = _clean_csv_text(raw.get("source_url"))
    return parsed


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


def _open_checked_csv_reader(
    fh: TextIO,
    csv_path: str,
    required_cols: set[str],
) -> csv.DictReader:
    reader = csv.DictReader(fh)
    fieldnames = set(reader.fieldnames or ())
    if not reader.fieldnames or not required_cols.issubset(fieldnames):
        raise ValueError(f"Invalid CSV header in {csv_path}: {reader.fieldnames!r}")
    return reader


def read_state_csv(csv_path: str) -> list[StateMonthResult]:
    """Load previously published rows from a per-state CSV if it exists."""
    if not os.path.isfile(csv_path):
        return []
    rows: list[StateMonthResult] = []
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for raw in _open_checked_csv_reader(fh, csv_path, {"state", "month"}):
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
        key = (item.state, item.month)
        prev = by_key.get(key)
        if item.source_url is None and prev is not None and prev.source_url is not None:
            item = replace(item, source_url=prev.source_url)
        by_key[key] = item
    return sorted(by_key.values(), key=lambda r: (r.month, r.state))


def result_to_csv_row(item: StateMonthResult) -> list[str]:
    """Serialize a ``StateMonthResult`` into the 15-column CSV row schema."""
    return [_clean_csv_text(item.state) or "", _clean_csv_text(item.month) or "", *_format_csv_metric_cells(item)]


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


@dataclass(**_DC_SLOTS)
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
    hold: float | None = None
    retail_hold: float | None = None
    mobile_hold: float | None = None
    count: int = 1

    def merge_fields(self, fields: Mapping[str, object]) -> None:
        self.count += 1
        self.hold = None
        self.retail_hold = None
        self.mobile_hold = None
        brand = str(fields["brand"])
        if self.brand == "Other / Retail Venue" and brand != "Other / Retail Venue":
            self.brand = brand
        source_url = fields.get("source_url")
        if not self.source_url and source_url:
            self.source_url = str(source_url)
        for name in _ACC_SUM_FIELDS:
            setattr(self, name, sum_optional(getattr(self, name), fields[name]))  # type: ignore[arg-type]


def _upsert_detail_group(
    grouped: dict[tuple[str, str, str], _DetailAccumulator],
    key: tuple[str, str, str],
    fields: Mapping[str, object],
) -> None:
    acc = grouped.get(key)
    if acc is None:
        grouped[key] = _DetailAccumulator(**fields)  # type: ignore[arg-type]
    else:
        acc.merge_fields(fields)


def _finalize_detail_group(
    grouped: dict[tuple[str, str, str], _DetailAccumulator],
) -> list[PublishedDetailRow]:
    rows = [
        PublishedDetailRow(
            state=state,
            month=month,
            operator=operator,
            brand=acc.brand,
            handle=acc.handle,
            revenue=acc.revenue,
            hold=acc.hold if (acc.count == 1 and acc.hold is not None) else hold_pct(acc.handle, acc.revenue),
            retail_handle=acc.retail_handle,
            retail_revenue=acc.retail_revenue,
            retail_hold=(
                acc.retail_hold
                if (acc.count == 1 and acc.retail_hold is not None)
                else hold_pct(acc.retail_handle, acc.retail_revenue)
            ),
            mobile_handle=acc.mobile_handle,
            mobile_revenue=acc.mobile_revenue,
            mobile_hold=(
                acc.mobile_hold
                if (acc.count == 1 and acc.mobile_hold is not None)
                else hold_pct(acc.mobile_handle, acc.mobile_revenue)
            ),
            source_url=acc.source_url,
            gross_revenue=acc.gross_revenue,
            promo_deductions=acc.promo_deductions,
            tax=acc.tax,
        )
        for (state, month, operator), acc in grouped.items()
    ]
    return sorted(rows, key=lambda r: (r.state, r.month, r.operator))


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
            if brand == "Other / Retail Venue":
                brand = classify_operator_brand(display_name, state=item.state)
            fields: dict[str, object] = {name: safe_float(getattr(op, name)) for name in _ACC_SUM_FIELDS}
            fields["brand"] = brand
            fields["source_url"] = _clean_csv_text(item.source_url)
            _upsert_detail_group(grouped, (item.state, item.month, display_name), fields)
    return _finalize_detail_group(grouped)


def read_detail_csv(csv_path: str) -> list[PublishedDetailRow]:
    """Load previously published detail rows from a per-state detail CSV, merging normalized duplicates."""
    if not os.path.isfile(csv_path):
        return []
    grouped: dict[tuple[str, str, str], _DetailAccumulator] = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for raw in _open_checked_csv_reader(fh, csv_path, {"state", "month", "operator"}):
            state = (raw.get("state") or "").strip()
            month = (raw.get("month") or "").strip()
            raw_operator = (raw.get("operator") or "").strip()
            if not state or not month or not raw_operator:
                continue
            operator = normalize_operator_name(raw_operator, state=state)
            if not operator:
                continue
            metrics = _parse_csv_metric_fields(raw)
            fields: dict[str, object] = {name: metrics[name] for name in _ACC_SUM_FIELDS}
            csv_brand = (raw.get("brand") or "").strip()
            fields["brand"] = (
                csv_brand
                if (csv_brand and csv_brand != "Other / Retail Venue")
                else classify_operator_brand(operator, state=state)
            )
            fields["source_url"] = metrics["source_url"]
            fields["hold"] = metrics["hold"]
            fields["retail_hold"] = metrics["retail_hold"]
            fields["mobile_hold"] = metrics["mobile_hold"]
            _upsert_detail_group(grouped, (state, month, operator), fields)
    return _finalize_detail_group(grouped)


def merge_detail_results(
    existing: Iterable[PublishedDetailRow],
    fresh: Iterable[PublishedDetailRow],
    *,
    refreshed_months: Iterable[tuple[str, str]] | None = None,
) -> list[PublishedDetailRow]:
    """Merge ``fresh`` detail rows onto ``existing``, replacing each refreshed ``(state, month)``."""
    fresh_list = list(fresh)
    replaced_months = {(row.state, row.month) for row in fresh_list}
    if refreshed_months is not None:
        replaced_months.update(refreshed_months)
    kept = [row for row in existing if (row.state, row.month) not in replaced_months]
    return sorted(kept + fresh_list, key=lambda r: (r.state, r.month, r.operator))


def detail_to_csv_row(item: PublishedDetailRow) -> list[str]:
    """Serialize a ``PublishedDetailRow`` into the 17-column detail CSV row schema."""
    return [
        _clean_csv_text(item.state) or "",
        _clean_csv_text(item.month) or "",
        _clean_csv_text(item.operator) or "",
        _clean_csv_text(item.brand) or "",
        *_format_csv_metric_cells(item),
    ]


def _write_sorted_csv(
    out_dir: str,
    filename: str,
    headers: Sequence[str],
    items: Iterable,
    *,
    sort_key: Callable[[object], object],
    row_fn: Callable[[object], Sequence[str]],
) -> str:
    ordered = sorted(items, key=sort_key)
    return _atomic_write_csv(os.path.join(out_dir, filename), headers, (row_fn(item) for item in ordered))


def write_state_csv(data_dir: str, state_slug: str, results: Sequence[StateMonthResult]) -> str:
    """Write ``<data_dir>/<state_slug>.csv`` sorted by ISO month and return its path."""
    return _write_sorted_csv(
        data_dir, f"{state_slug}.csv", CSV_HEADERS, results,
        sort_key=lambda r: r.month, row_fn=result_to_csv_row,
    )


def write_combined_csv(data_dir: str, all_results: Iterable[StateMonthResult]) -> str:
    """Write ``<data_dir>/all.csv`` sorted by ``(state, month)`` and return its path."""
    return _write_sorted_csv(
        data_dir, "all.csv", CSV_HEADERS, all_results,
        sort_key=lambda r: (r.state, r.month), row_fn=result_to_csv_row,
    )


def write_state_detail_csv(details_dir: str, state_slug: str, rows: Sequence[PublishedDetailRow]) -> str:
    """Write ``<details_dir>/<state_slug>.csv`` sorted by ``(month, operator)``."""
    return _write_sorted_csv(
        details_dir, f"{state_slug}.csv", DETAIL_CSV_HEADERS, rows,
        sort_key=lambda r: (r.month, r.operator), row_fn=detail_to_csv_row,
    )


def write_combined_detail_csv(details_dir: str, all_rows: Iterable[PublishedDetailRow]) -> str:
    """Write ``<details_dir>/all.csv`` sorted by ``(state, month, operator)``."""
    return _write_sorted_csv(
        details_dir, "all.csv", DETAIL_CSV_HEADERS, all_rows,
        sort_key=lambda r: (r.state, r.month, r.operator), row_fn=detail_to_csv_row,
    )
