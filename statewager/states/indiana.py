"""Indiana sports wagering regulatory fetcher and parser (IGC)."""
from __future__ import annotations

from datetime import date, datetime
import io
import os
from typing import Callable

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, load_cached_or_fetch
from ..discover import DiscoveryReport
from ..http import probe_url_variants
from ..model import (
    OperatorMonthResult,
    StateMonthResult,
    build_state_logger,
    format_iso_month,
    hold_pct,
    log_skip,
    reconcile_components,
    resolve_fetch_months,
    validate_result,
)
from ..parse import is_number

STATE_NAME = "Indiana"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

USER_AGENT = "Mozilla/5.0"
URL_VARIANT_TEMPLATES = [
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}-{mm}-Revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}_{mm}_Revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}-{mm}-revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{mm}-{yyyy}-Revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}{mm}_Revenue.xlsx",
]
_DEFAULT_LOG = build_state_logger("IN")


def download_xlsx(
    yyyy: str,
    mm: str,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    return probe_url_variants(
        URL_VARIANT_TEMPLATES,
        {"yyyy": yyyy, "mm": mm},
        signature_check=b"PK",
        wayback_timestamp=f"{yyyy}{mm}",
        log=log,
        report=report,
        report_key=f"{yyyy}-{mm}",
    )


def normalize_sheet_title(title: str) -> str:
    return "".join(ch for ch in title.lower().strip() if ch.isalnum() or ch.isspace()).strip()


def parse_summary_totals(workbook) -> tuple[float, float] | None:
    candidate_sheets = [
        ws
        for ws in workbook.worksheets
        if "sw" in ws.title.lower() and "summary" in ws.title.lower()
    ] or workbook.worksheets
    for sheet in candidate_sheets:
        handle_col = None
        agr_col = None
        data_rows: list[tuple[object, ...]] = []
        for row in sheet.iter_rows(values_only=True):
            row = tuple(row)
            if not any(cell is not None for cell in row):
                continue
            data_rows.append(row)
            if handle_col is None or agr_col is None:
                for index, cell in enumerate(row):
                    text = str(cell or "").strip().lower()
                    if handle_col is None and text == "handle":
                        handle_col = index
                    if agr_col is None and "taxable agr" in text:
                        agr_col = index
        if handle_col is None or agr_col is None:
            continue
        for row in data_rows:
            label = str(row[0] if row else "").strip().lower()
            if label != "total":
                continue
            handle = row[handle_col] if handle_col < len(row) else None
            revenue = row[agr_col] if agr_col < len(row) else None
            if is_number(handle) and is_number(revenue):
                return float(handle), float(revenue)
    return None


def parse_location_totals(workbook) -> dict[str, tuple[float, float]]:
    def is_location_totals_sheet(sheet) -> bool:
        normalized_title = normalize_sheet_title(sheet.title.strip())
        if "sw tax summary" in sheet.title.strip().lower() or normalized_title in {"sheet7", "7 sw tax summary"}:
            return True
        for row in sheet.iter_rows(min_row=1, max_row=6, values_only=True):
            for cell in row:
                if "summary of sports wagering tax" in str(cell or "").strip().lower():
                    return True
        return False

    sheet = next((ws for ws in workbook.worksheets if is_location_totals_sheet(ws)), None)
    if sheet is None:
        return {}
    rows: dict[str, tuple[float, float]] = {}
    saw_location_rows = False
    for row in sheet.iter_rows(values_only=True):
        label = str(row[0] if row else "").strip()
        if not label:
            continue
        if label.upper() == "TOTAL":
            if saw_location_rows:
                break
            continue
        if label.startswith("*"):
            continue
        if not (
            len(row) > 5
            and str(row[3] if len(row) > 3 else "").strip().lower() == "handle"
            and "taxable agr" in str(row[5] if len(row) > 5 else "").strip().lower()
        ):
            handle = row[1] if len(row) > 1 else None
            revenue = row[2] if len(row) > 2 else None
        else:
            continue
        if not (is_number(handle) and is_number(revenue)) and len(row) > 5:
            handle = row[3] if len(row) > 3 else None
            revenue = row[5] if len(row) > 5 else None
        if is_number(handle) and is_number(revenue):
            saw_location_rows = True
            rows[label] = (float(handle), float(revenue))
    return rows


def is_retail_detail_label(label: str) -> bool:
    lower = label.strip().lower()
    return lower == "retail" or lower.startswith("wc ") or "winner" in lower


def allocate_adjustments(retail_revenue: float, mobile_revenue: float, adjustments: float) -> tuple[float, float]:
    total = retail_revenue + mobile_revenue
    if total == 0:
        return retail_revenue + adjustments, mobile_revenue
    retail_share = retail_revenue / total if retail_revenue else 0.0
    retail_adjustment = adjustments * retail_share
    return retail_revenue + retail_adjustment, mobile_revenue + (adjustments - retail_adjustment)


def parse_detail_breakdown(workbook) -> dict[str, tuple[float, float, float, float]] | None:
    detail_sheet = next(
        (
            ws
            for ws in workbook.worksheets
            if "sw details" in ws.title.strip().lower()
            or normalize_sheet_title(ws.title.strip()) in {"sheet8", "8 sw details"}
        ),
        None,
    )
    if detail_sheet is None:
        return None
    breakdowns: dict[str, tuple[float, float, float, float]] = {}
    rows = [tuple(r) for r in detail_sheet.iter_rows(values_only=True)]
    for row_index, row in enumerate(rows):
        values = list(row)
        for offset in (0, 5, 10):
            if offset + 3 >= len(values):
                continue
            header = str(values[offset] or "").strip()
            metric = str(values[offset + 1] or "").strip().lower()
            revenue_header = str(values[offset + 3] or "").strip().lower()
            if not header or metric != "handle" or revenue_header != "gross receipts":
                continue
            location_name = header
            retail_handle = 0.0
            retail_revenue = 0.0
            mobile_handle = 0.0
            mobile_revenue = 0.0
            block_retail_handle = 0.0
            block_retail_revenue = 0.0
            block_mobile_handle = 0.0
            block_mobile_revenue = 0.0
            block_adjustments = 0.0
            for detail_row in rows[row_index + 1:]:
                label = str(detail_row[offset] if offset < len(detail_row) else "" or "").strip()
                if not label:
                    break
                lower = label.lower()
                if str(detail_row[offset + 1] if offset + 1 < len(detail_row) else "" or "").strip().lower() == "handle":
                    break
                handle = detail_row[offset + 1] if offset + 1 < len(detail_row) else None
                revenue = detail_row[offset + 3] if offset + 3 < len(detail_row) else None
                if lower == "adjustments":
                    if is_number(revenue):
                        block_adjustments += float(revenue)
                    continue
                if lower == "taxable agr":
                    block_retail_revenue, block_mobile_revenue = allocate_adjustments(
                        block_retail_revenue,
                        block_mobile_revenue,
                        block_adjustments,
                    )
                    retail_handle += block_retail_handle
                    retail_revenue += block_retail_revenue
                    mobile_handle += block_mobile_handle
                    mobile_revenue += block_mobile_revenue
                    breakdowns[location_name] = (
                        retail_handle,
                        retail_revenue,
                        mobile_handle,
                        mobile_revenue,
                    )
                    break
                if is_retail_detail_label(label):
                    if is_number(handle):
                        block_retail_handle += float(handle)
                    if is_number(revenue):
                        block_retail_revenue += float(revenue)
                else:
                    if is_number(handle):
                        block_mobile_handle += float(handle)
                    if is_number(revenue):
                        block_mobile_revenue += float(revenue)
    return breakdowns


def parse_state_totals(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
] | None:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Indiana workbooks")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open workbook: {exc}")
        return None
    try:
        totals = parse_summary_totals(workbook)
        breakdown = parse_detail_breakdown(workbook)
        locations = parse_location_totals(workbook)
        if totals is None:
            log("WARN Indiana workbook missing statewide summary totals")
            return None
        if breakdown is None:
            log("WARN Indiana workbook missing operator retail/mobile breakdown")
            return None
        handle, revenue = totals
        retail_handle = sum(values[0] for values in breakdown.values())
        retail_revenue = sum(values[1] for values in breakdown.values())
        mobile_handle = handle - retail_handle
        mobile_revenue = revenue - retail_revenue
        detail_rows: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
        for location_name, (location_handle, location_revenue) in locations.items():
            split = breakdown.get(location_name)
            if split is None:
                detail_rows.append((location_name, location_handle, location_revenue, None, None, None, None))
                continue
            detail_rows.append(
                (
                    location_name,
                    location_handle,
                    location_revenue,
                    split[0],
                    split[1],
                    split[2],
                    split[3],
                )
            )
        return handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, detail_rows
    finally:
        workbook.close()


def parse_local_file(local_dir: str, yyyy: str, mm: str) -> bytes | None:
    prefix = f"{yyyy}-{mm}-revenue"
    for entry in sorted(os.listdir(local_dir)):
        if entry.lower().endswith(".xlsx") and entry.lower().startswith(prefix.lower()):
            with open(os.path.join(local_dir, entry), "rb") as file_handle:
                return file_handle.read()
    return None


def fetch(
    month_start: str | date | datetime | tuple[int, int],
    month_end: str | date | datetime | tuple[int, int] | None = None,
    *,
    cache_dir: str | None = None,
    local_dir: str | None = None,
    offline: bool = False,
    log: Callable[[str], None] | None = None,
    report: DiscoveryReport | None = None,
) -> list[StateMonthResult]:
    """Fetch and parse Indiana monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Indiana sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    current_year = date.today().year
    results: list[StateMonthResult] = []
    for mdate in target_dates:
        yyyy = mdate.strftime("%Y")
        mm = mdate.strftime("%m")
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(mdate.year, mdate.month)
        source_url = URL_VARIANT_TEMPLATES[0].format(yyyy=yyyy, mm=mm)
        try:
            logger(f"INFO processing {label}")
            data = parse_local_file(local_dir, yyyy, mm) if local_dir else None
            if data:
                logger(f"INFO parsed local workbook for {label}")
            if data is None:
                path = cache_path(cache_dir, f"indiana_{yyyy}_{mm}.xlsx")
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda y=yyyy, m=mm: download_xlsx(y, m, report=report, log=logger),
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {label}",
                    fetch_error_message=f"could not download Indiana workbook for {label}",
                    cache_ttl_seconds=7 * 86400 if int(yyyy) >= current_year else None,
                    offline=offline,
                )
            if data is None:
                log_skip(logger, label, "workbook unavailable")
                continue
            parsed = parse_state_totals(data, log=logger)
            if parsed is None:
                log_skip(logger, label, "could not parse statewide totals")
                continue
            handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, locations = parsed
            retail_handle, retail_revenue, mobile_handle, mobile_revenue = reconcile_components(
                label,
                handle,
                revenue,
                retail_handle,
                retail_revenue,
                mobile_handle,
                mobile_revenue,
            )
            validated = validate_result(label, handle, revenue)
            if validated is None:
                continue
            handle, revenue = validated
            hold = hold_pct(handle, revenue)
            retail_hold = hold_pct(retail_handle, retail_revenue) if retail_handle else None
            mobile_hold = hold_pct(mobile_handle, mobile_revenue) if mobile_handle else None
            operator_details: list[OperatorMonthResult] = []
            for (
                location_name,
                location_handle,
                location_revenue,
                location_retail_handle,
                location_retail_revenue,
                location_mobile_handle,
                location_mobile_revenue,
            ) in locations:
                location_validated = validate_result(f"{label} {location_name}", location_handle, location_revenue)
                if location_validated is None:
                    logger(f"WARN skipped detail row for {label} {location_name}; invalid handle/revenue")
                    continue
                location_handle, location_revenue = location_validated
                location_hold = hold_pct(location_handle, location_revenue)
                operator_details.append(
                    OperatorMonthResult(
                        month=iso_month,
                        operator=location_name,
                        handle=location_handle,
                        revenue=location_revenue,
                        hold=location_hold,
                        retail_handle=location_retail_handle,
                        retail_revenue=location_retail_revenue,
                        mobile_handle=location_mobile_handle,
                        mobile_revenue=location_mobile_revenue,
                    )
                )
            results.append(
                StateMonthResult(
                    state=STATE_NAME,
                    month=iso_month,
                    handle=handle,
                    revenue=revenue,
                    hold=hold,
                    retail_handle=retail_handle,
                    retail_revenue=retail_revenue,
                    retail_hold=retail_hold,
                    mobile_handle=mobile_handle,
                    mobile_revenue=mobile_revenue,
                    mobile_hold=mobile_hold,
                    source_url=source_url,
                    details=operator_details,
                )
            )
            logger(
                f"INFO {label}: handle={handle:,.0f} revenue={revenue:,.0f} "
                f"retail_handle={retail_handle:,.0f} mobile_handle={mobile_handle:,.0f} hold={hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
