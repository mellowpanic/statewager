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

from ..cache import cache_path, load_cached_or_fetch, refresh_cached_if_stale
from ..discover import DiscoveryReport
from ..http import probe_url_variants
from ..model import (
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
)
from ..parse import find_sheet_fuzzy, is_number

STATE_NAME = "Ohio"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

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
    *,
    matched_url: list[str] | None = None,
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
        matched_url=matched_url,
    )


def _parse_month_sheet_with_fiscal(
    sheet,
) -> tuple[
    tuple[
        float,
        float,
        float,
        float,
        float,
        float,
        list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
    ],
    float,
    float | None,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None, float | None, float | None, float | None]],
] | None:
    online_handle = None
    online_revenue = None
    retail_handle = None
    retail_revenue = None
    promo_col = 4
    details: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
    details_fiscal: list[
        tuple[str, float, float, float | None, float | None, float | None, float | None, float | None, float | None, float | None]
    ] = []
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
            or lower_label.startswith("all proprietors / services providers")
        ):
            for idx, cell in enumerate(row):
                if "promotional" in str(cell or "").lower():
                    promo_col = idx
                    break
            continue
        if "subtotal type a online" in row_text:
            numeric = [float(cell) for cell in row if is_number(cell)]
            if len(row) > 5 and is_number(row[1]) and is_number(row[5]):
                online_handle, online_revenue = float(row[1]), float(row[5])
            elif len(numeric) >= 5:
                online_handle, online_revenue = numeric[0], numeric[4]
            section = None
        elif "subtotal type b retail" in row_text:
            numeric = [float(cell) for cell in row if is_number(cell)]
            if len(row) > 5 and is_number(row[1]) and is_number(row[5]):
                retail_handle, retail_revenue = float(row[1]), float(row[5])
            elif len(numeric) >= 5:
                retail_handle, retail_revenue = numeric[0], numeric[4]
            section = None
        elif "total online and retail" in row_text:
            numeric = [float(cell) for cell in row if is_number(cell)]
            if len(row) > 5 and is_number(row[1]) and is_number(row[5]):
                total_handle, total_revenue = float(row[1]), float(row[5])
                total_promo = float(row[promo_col]) if len(row) > promo_col and is_number(row[promo_col]) else None
            elif len(numeric) >= 5:
                total_handle, total_revenue = numeric[0], numeric[4]
                total_promo = numeric[3] if len(numeric) >= 6 else None
            else:
                continue
            if None not in (online_handle, online_revenue, retail_handle, retail_revenue):
                core = (
                    total_handle,
                    total_revenue,
                    retail_handle,
                    retail_revenue,
                    online_handle,
                    online_revenue,
                    details,
                )
                return core, total_revenue, total_promo, details_fiscal
        elif section and label and is_number(row[1] if len(row) > 1 else None):
            revenue = row[5] if len(row) > 5 else None
            if is_number(revenue):
                handle = float(row[1])
                revenue_value = float(revenue)
                promo_val = float(row[promo_col]) if len(row) > promo_col and is_number(row[promo_col]) else None
                rh = handle if section == "retail" else None
                rr = revenue_value if section == "retail" else None
                mh = handle if section == "online" else None
                mr = revenue_value if section == "online" else None
                details.append((label, handle, revenue_value, rh, rr, mh, mr))
                details_fiscal.append((label, handle, revenue_value, rh, rr, mh, mr, revenue_value, promo_val, None))
    return None


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
    parsed = _parse_month_sheet_with_fiscal(sheet)
    return parsed[0] if parsed is not None else None


def _parse_cumulative_xlsx_with_fiscal(
    data: bytes,
    target_months: list[tuple[str, str, str, str]],
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    dict[
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
    ],
    dict[
        str,
        tuple[
            float,
            float | None,
            list[tuple[str, float, float, float | None, float | None, float | None, float | None, float | None, float | None, float | None]],
        ],
    ],
]:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Ohio workbooks")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open workbook: {exc}")
        return {}, {}
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
        fiscal_results: dict[
            str,
            tuple[
                float,
                float | None,
                list[tuple[str, float, float, float | None, float | None, float | None, float | None, float | None, float | None, float | None]],
            ],
        ] = {}
        for month_name, _, _, label in target_months:
            sheet = find_sheet_fuzzy(workbook, month_name)
            if sheet is None:
                continue
            parsed = _parse_month_sheet_with_fiscal(sheet)
            if parsed is not None:
                core, total_gross, total_promo, details_fiscal = parsed
                results[label] = core
                fiscal_results[label] = (total_gross, total_promo, details_fiscal)
        return results, fiscal_results
    finally:
        workbook.close()


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
    results, _ = _parse_cumulative_xlsx_with_fiscal(data, target_months, log=log)
    return results


def _parse_local_files_with_fiscal(
    local_dir: str,
    year: int,
    target_months: list[tuple[str, str, str, str]],
    log: Callable[[str], None] = _DEFAULT_LOG,
):
    for entry in sorted(os.listdir(local_dir), reverse=True):
        if not entry.lower().endswith(".xlsx"):
            continue
        if str(year) not in entry:
            continue
        with open(os.path.join(local_dir, entry), "rb") as file_handle:
            data = file_handle.read()
        if data.startswith(b"PK"):
            log(f"INFO using local workbook {entry}")
            return _parse_cumulative_xlsx_with_fiscal(data, target_months, log=log)
    return {}, {}


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
    results, _ = _parse_local_files_with_fiscal(local_dir, year, target_months, log=log)
    return results


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
    fiscal_by_label: dict[
        str,
        tuple[
            float,
            float | None,
            list[tuple[str, float, float, float | None, float | None, float | None, float | None, float | None, float | None, float | None]],
        ],
    ] = {}
    source_by_year: dict[int, str] = {}
    current_year = date.today().year
    for year in sorted({int(yr) for _, yr, _, _ in months}):
        try:
            year_months = [entry for entry in months if int(entry[1]) == year]
            latest_report_number = max(int(month_num) for _, _, month_num, _ in year_months)
            found_urls: list[str] = []

            def _download_oh(
                y: int = year,
                lrn: int = latest_report_number,
                urls: list[str] = found_urls,
            ) -> bytes | None:
                try:
                    return download_cumulative_workbook(y, lrn, report=report, log=logger, matched_url=urls)
                except TypeError as exc:
                    if "matched_url" not in str(exc):
                        raise
                    return download_cumulative_workbook(y, lrn, report=report, log=logger)

            parsed, parsed_fiscal = (
                _parse_local_files_with_fiscal(local_dir, year, year_months, log=logger) if local_dir else ({}, {})
            )
            if not parsed:
                path = _resolve_ohio_cache_path(cache_dir, year)
                was_cached = bool(path and os.path.isfile(path))
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=_download_oh,
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {year}",
                    fetch_error_message=f"could not download Ohio cumulative workbook for {year}",
                    validate=lambda payload: payload.startswith(b"PK"),
                    cache_ttl_seconds=7 * 86400 if year >= current_year else None,
                    offline=offline,
                )
                if data:
                    parsed, parsed_fiscal = _parse_cumulative_xlsx_with_fiscal(data, year_months, log=logger)
                latest_requested = max(date(int(yr), int(mo), 1) for _, yr, mo, _ in year_months)
                if (
                    was_cached
                    and not offline
                    and path
                    and len(parsed) < len(year_months)
                    and (date.today() - latest_requested).days <= 90
                ):
                    refreshed = refresh_cached_if_stale(
                        path,
                        data,
                        fetcher=_download_oh,
                        validate=lambda payload: payload.startswith(b"PK"),
                        offline=offline,
                        log=logger,
                    )
                    if refreshed is not None:
                        parsed, parsed_fiscal = _parse_cumulative_xlsx_with_fiscal(refreshed, year_months, log=logger)
            source_by_year[year] = (
                found_urls[-1] if found_urls else candidate_workbook_urls(year, latest_report_number)[0]
            )
            parsed_by_label.update(parsed)
            fiscal_by_label.update(parsed_fiscal)
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
        total_gross, total_promo, details_fiscal = fiscal_by_label.get(label, (None, None, details))
        operator_details = build_operator_results(iso_month, details_fiscal or details)
        item = finalize_state_month(
            state=STATE_NAME,
            iso_month=iso_month,
            handle=total_handle,
            revenue=total_revenue,
            retail_handle=retail_handle,
            retail_revenue=retail_revenue,
            mobile_handle=mobile_handle,
            mobile_revenue=mobile_revenue,
            source_url=source_by_year.get(mdate.year),
            details=operator_details,
            gross_revenue=total_gross,
            promo_deductions=total_promo,
            log=logger,
            label=label,
        )
        if item is None:
            continue
        results.append(item)
        logger(
            f"INFO {label}: handle={item.handle:,.0f} revenue={item.revenue:,.0f} "
            f"retail_handle={f'{item.retail_handle:,.0f}' if item.retail_handle is not None else 'n/a'} "
            f"mobile_handle={f'{item.mobile_handle:,.0f}' if item.mobile_handle is not None else 'n/a'} "
            f"hold={(item.hold or 0.0):.1f}%"
        )
    return results
