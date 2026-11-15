"""CSV serialization helpers for StateMonthResult datasets."""
from __future__ import annotations

from dataclasses import dataclass
import csv
import math
import os
import tempfile
from typing import Callable, Iterable, Sequence, TextIO

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
                    gross_revenue=_parse_opt_float(raw.get("gross_revenue")),
                    promo_deductions=_parse_opt_float(raw.get("promo_deductions")),
                    tax=_parse_opt_float(raw.get("tax")),
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
    """Serialize a ``StateMonthResult`` into the 15-column CSV row schema."""
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
        _format_amount(item.gross_revenue),
        _format_amount(item.promo_deductions),
        _format_amount(item.tax),
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


def _make_detail_row(
    *,
    state: str,
    month: str,
    operator: str,
    brand: str,
    handle: float | None,
    revenue: float | None,
    retail_handle: float | None,
    retail_revenue: float | None,
    mobile_handle: float | None,
    mobile_revenue: float | None,
    source_url: str | None,
    gross_revenue: float | None = None,
    promo_deductions: float | None = None,
    tax: float | None = None,
) -> PublishedDetailRow:
    return PublishedDetailRow(
        state=state,
        month=month,
        operator=operator,
        brand=brand,
        handle=handle,
        revenue=revenue,
        hold=hold_pct(handle, revenue),
        retail_handle=retail_handle,
        retail_revenue=retail_revenue,
        retail_hold=hold_pct(retail_handle, retail_revenue) if retail_handle else None,
        mobile_handle=mobile_handle,
        mobile_revenue=mobile_revenue,
        mobile_hold=hold_pct(mobile_handle, mobile_revenue) if mobile_handle else None,
        source_url=source_url,
        gross_revenue=gross_revenue,
        promo_deductions=promo_deductions,
        tax=tax,
    )


def extract_published_details(results: Iterable[StateMonthResult]) -> list[PublishedDetailRow]:
    """Normalize and aggregate operator/venue details across ``StateMonthResult`` records."""
    grouped: dict[tuple[str, str, str], dict[str, object]] = {}
    for item in results:
        source_details = item.publish_details if item.publish_details else item.details
        for op in source_details:
            display_name = normalize_operator_name(op.operator, state=item.state)
            if not display_name:
                continue
            brand = classify_operator_brand(op.operator, state=item.state)
            key = (item.state, item.month, display_name)
            bucket = grouped.get(key)
            if bucket is None:
                grouped[key] = {
                    "brand": brand,
                    "handle": safe_float(op.handle),
                    "revenue": safe_float(op.revenue),
                    "retail_handle": safe_float(op.retail_handle),
                    "retail_revenue": safe_float(op.retail_revenue),
                    "mobile_handle": safe_float(op.mobile_handle),
                    "mobile_revenue": safe_float(op.mobile_revenue),
                    "source_url": item.source_url,
                    "gross_revenue": safe_float(op.gross_revenue),
                    "promo_deductions": safe_float(op.promo_deductions),
                    "tax": safe_float(op.tax),
                }
            else:
                if bucket["brand"] == "Other / Retail Venue" and brand != "Other / Retail Venue":
                    bucket["brand"] = brand
                bucket["handle"] = _sum_opt(bucket["handle"], safe_float(op.handle))  # type: ignore[arg-type]
                bucket["revenue"] = _sum_opt(bucket["revenue"], safe_float(op.revenue))  # type: ignore[arg-type]
                bucket["retail_handle"] = _sum_opt(bucket["retail_handle"], safe_float(op.retail_handle))  # type: ignore[arg-type]
                bucket["retail_revenue"] = _sum_opt(bucket["retail_revenue"], safe_float(op.retail_revenue))  # type: ignore[arg-type]
                bucket["mobile_handle"] = _sum_opt(bucket["mobile_handle"], safe_float(op.mobile_handle))  # type: ignore[arg-type]
                bucket["mobile_revenue"] = _sum_opt(bucket["mobile_revenue"], safe_float(op.mobile_revenue))  # type: ignore[arg-type]
                bucket["gross_revenue"] = _sum_opt(bucket["gross_revenue"], safe_float(op.gross_revenue))  # type: ignore[arg-type]
                bucket["promo_deductions"] = _sum_opt(bucket["promo_deductions"], safe_float(op.promo_deductions))  # type: ignore[arg-type]
                bucket["tax"] = _sum_opt(bucket["tax"], safe_float(op.tax))  # type: ignore[arg-type]

    rows = [
        _make_detail_row(
            state=state,
            month=month,
            operator=operator,
            brand=str(bucket["brand"]),
            handle=bucket["handle"],  # type: ignore[arg-type]
            revenue=bucket["revenue"],  # type: ignore[arg-type]
            retail_handle=bucket["retail_handle"],  # type: ignore[arg-type]
            retail_revenue=bucket["retail_revenue"],  # type: ignore[arg-type]
            mobile_handle=bucket["mobile_handle"],  # type: ignore[arg-type]
            mobile_revenue=bucket["mobile_revenue"],  # type: ignore[arg-type]
            source_url=bucket["source_url"],  # type: ignore[arg-type]
            gross_revenue=bucket["gross_revenue"],  # type: ignore[arg-type]
            promo_deductions=bucket["promo_deductions"],  # type: ignore[arg-type]
            tax=bucket["tax"],  # type: ignore[arg-type]
        )
        for (state, month, operator), bucket in grouped.items()
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
                    gross_revenue=_parse_opt_float(raw.get("gross_revenue")),
                    promo_deductions=_parse_opt_float(raw.get("promo_deductions")),
                    tax=_parse_opt_float(raw.get("tax")),
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
    return [
        item.state,
        item.month,
        item.operator,
        item.brand,
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
