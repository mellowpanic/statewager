"""Ohio sports wagering regulatory fetcher and parser (OCCC)."""
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
from ..parse import find_sheet_fuzzy, is_number

STATE_NAME = "Ohio"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

USER_AGENT = "Mozilla/5.0"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3

_DEFAULT_LOG = build_state_logger("OH")


def candidate_workbook_urls(year: int, latest_report_number: int) -> list[str]:
    """Return cumulative-XLSX URLs to try, newest naming first."""
    urls: list[str] = []
    for report_number in range(latest_report_number, 0, -1):
        urls.append(
            "https://dam.assets.ohio.gov/raw/upload/"
            f"casinocontrol.ohio.gov/revenue-reports/{year}/Sports/"
            f"{year}_Sports_Gaming_Revenue_Report{report_number:02d}.xlsx"
        )
    urls.append(
        "https://dam.assets.ohio.gov/raw/upload/"
        f"casinocontrol.ohio.gov/revenue-reports/{year}/Sports/"
        f"{year}_Sports_Gaming_Revenue_Report.xlsx"
    )
    urls.append(
        "https://dam.assets.ohio.gov/raw/upload/"
        f"casinocontrol.ohio.gov/revenue-reports/{year}/"
        f"{year}_Sports_Gaming_Revenue_Report.xlsx"
    )
    urls.append(
        "https://dam.assets.ohio.gov/raw/upload/"
        "casinocontrol.ohio.gov/revenue-reports/Full%20Year%20Revenue%20Reports/"
        f"December%20{year}%20Ohio%20Sports%20Gaming%20Revenue%20Report.xlsx"
    )
    return urls


def download_cumulative_workbook(
    year: int,
    latest_report_number: int,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    urls = candidate_workbook_urls(year, latest_report_number)
    return probe_url_variants(
        urls,
        {},
        signature_check=b"PK",
        wayback_timestamp=f"{year}12",
        log=log,
        report=report,
        report_key=f"{year}:cumulative",
    )


def parse_month_sheet(
    sheet,
) -> tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
] | None:
    online_handle = None
    online_revenue = None
    retail_handle = None
    retail_revenue = None
    details: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
    section = None
    for row in sheet.iter_rows(values_only=True):
        row_text = " ".join(str(cell or "") for cell in row).lower()
        label = str(row[0] if row else "").strip()
        lower_label = label.lower()
        if "type a proprietors - online sports gaming" in row_text:
            section = "online"
            continue
        if "type b proprietors - retail sports gaming" in row_text:
            section = "retail"
            continue
        if (
            lower_label.startswith("online proprietor / services provider")
            or lower_label.startswith("retail proprietor / services provider")
        ):
            continue
        if "subtotal type a online" in row_text:
            numeric = [float(cell) for cell in row if is_number(cell)]
            online_handle, online_revenue = numeric[0], numeric[4]
            section = None
        elif "subtotal type b retail" in row_text:
            numeric = [float(cell) for cell in row if is_number(cell)]
            retail_handle, retail_revenue = numeric[0], numeric[4]
            section = None
        elif "total online and retail" in row_text:
            numeric = [float(cell) for cell in row if is_number(cell)]
            total_handle, total_revenue = numeric[0], numeric[4]
            if None not in (online_handle, online_revenue, retail_handle, retail_revenue):
                return (
                    total_handle,
                    total_revenue,
                    retail_handle,
                    retail_revenue,
                    online_handle,
                    online_revenue,
                    details,
                )
        elif section and label and is_number(row[1] if len(row) > 1 else None):
            revenue = row[5] if len(row) > 5 else None
            if is_number(revenue):
                handle = float(row[1])
                revenue_value = float(revenue)
                details.append(
                    (
                        label,
                        handle,
                        revenue_value,
                        handle if section == "retail" else None,
                        revenue_value if section == "retail" else None,
                        handle if section == "online" else None,
                        revenue_value if section == "online" else None,
                    )
                )
    return None


def parse_cumulative_xlsx(
    data: bytes,
    target_months: list[tuple[str, str, str, str]],
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[
    str,
    tuple[
        float,
        float,
        float,
        float,
        float,
        float,
        list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
    ],
]:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Ohio workbooks")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open workbook: {exc}")
        return {}
    try:
        results: dict[
            str,
            tuple[
                float,
                float,
                float,
                float,
                float,
                float,
                list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
            ],
        ] = {}
        for month_name, _, _, label in target_months:
            sheet = find_sheet_fuzzy(
                workbook,
                month_name.upper(),
                month_name.capitalize(),
                month_name,
                month_name.title(),
            )
            if sheet is None:
                continue
            parsed = parse_month_sheet(sheet)
            if parsed is not None:
                results[label] = parsed
        return results
    finally:
        workbook.close()


def parse_local_files(
    local_dir: str,
    year: int,
    target_months: list[tuple[str, str, str, str]],
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[
    str,
    tuple[
        float,
        float,
        float,
        float,
        float,
        float,
        list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
    ],
]:
    for entry in sorted(os.listdir(local_dir), reverse=True):
        if not entry.lower().endswith(".xlsx"):
            continue
        if str(year) not in entry:
            continue
        with open(os.path.join(local_dir, entry), "rb") as file_handle:
            data = file_handle.read()
        if data.startswith(b"PK"):
            log(f"INFO using local workbook {entry}")
            return parse_cumulative_xlsx(data, target_months, log=log)
    return {}


def _resolve_ohio_cache_path(cache_dir: str | None, year: int) -> str | None:
    primary = cache_path(cache_dir, f"ohio_{year}_ytd.xlsx")
    if primary and os.path.isfile(primary):
        return primary
    nested = cache_path(cache_dir, os.path.join("ohio", f"ohio_{year}_ytd.xlsx"))
    if nested and os.path.isfile(nested):
        return nested
    return primary


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
    """Fetch and parse Ohio monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Ohio sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    months = [
        (
            mdate.strftime("%B"),
            mdate.strftime("%Y"),
            mdate.strftime("%m"),
            mdate.strftime("%b %Y"),
        )
        for mdate in target_dates
    ]
    parsed_by_label: dict[
        str,
        tuple[
            float,
            float,
            float,
            float,
            float,
            float,
            list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
        ],
    ] = {}
    source_by_year: dict[int, str] = {}
    current_year = date.today().year
    for year in sorted({int(yr) for _, yr, _, _ in months}):
        try:
            year_months = [entry for entry in months if int(entry[1]) == year]
            latest_report_number = max(int(month_num) for _, _, month_num, _ in year_months)
            source_by_year[year] = candidate_workbook_urls(year, latest_report_number)[0]
            parsed = parse_local_files(local_dir, year, year_months, log=logger) if local_dir else {}
            if not parsed:
                path = _resolve_ohio_cache_path(cache_dir, year)
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda y=year, lrn=latest_report_number: download_cumulative_workbook(
                        y, lrn, report=report, log=logger,
                    ),
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {year}",
                    fetch_error_message=f"could not download Ohio cumulative workbook for {year}",
                    validate=lambda payload: payload.startswith(b"PK"),
                    cache_ttl_seconds=7 * 86400 if year >= current_year else None,
                    offline=offline,
                )
                if data:
                    parsed = parse_cumulative_xlsx(data, year_months, log=logger)
            parsed_by_label.update(parsed)
        except Exception as exc:
            logger(f"WARN failed Ohio workbook processing for {year}: {exc}")

    results: list[StateMonthResult] = []
    for mdate, (_, _, _, label) in zip(target_dates, months):
        iso_month = format_iso_month(mdate.year, mdate.month)
        if label not in parsed_by_label:
            log_skip(logger, label, "no parsed value")
            continue
        (
            total_handle,
            total_revenue,
            retail_handle,
            retail_revenue,
            mobile_handle,
            mobile_revenue,
            details,
        ) = parsed_by_label[label]
        validated = validate_result(label, total_handle, total_revenue)
        if validated is None:
            continue
        handle, revenue = validated
        retail_handle, retail_revenue, mobile_handle, mobile_revenue = reconcile_components(
            label,
            handle,
            revenue,
            retail_handle,
            retail_revenue,
            mobile_handle,
            mobile_revenue,
        )
        hold = hold_pct(handle, revenue)
        retail_hold = hold_pct(retail_handle, retail_revenue) if retail_handle else None
        mobile_hold = hold_pct(mobile_handle, mobile_revenue) if mobile_handle else None
        operator_details: list[OperatorMonthResult] = []
        for (
            operator_name,
            operator_handle,
            operator_revenue,
            operator_retail_handle,
            operator_retail_revenue,
            operator_mobile_handle,
            operator_mobile_revenue,
        ) in details:
            operator_details.append(
                OperatorMonthResult(
                    month=iso_month,
                    operator=operator_name,
                    handle=operator_handle,
                    revenue=operator_revenue,
                    hold=hold_pct(operator_handle, operator_revenue),
                    retail_handle=operator_retail_handle,
                    retail_revenue=operator_retail_revenue,
                    mobile_handle=operator_mobile_handle,
                    mobile_revenue=operator_mobile_revenue,
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
                source_url=source_by_year.get(mdate.year),
                details=operator_details,
            )
        )
        logger(
            f"INFO {label}: handle={handle:,.0f} revenue={revenue:,.0f} "
            f"retail_handle={retail_handle:,.0f} mobile_handle={mobile_handle:,.0f} hold={hold:.1f}%"
        )
    return results
