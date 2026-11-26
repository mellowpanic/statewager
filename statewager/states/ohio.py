"""Ohio sports wagering regulatory fetcher and parser (OCCC)."""
from __future__ import annotations

from datetime import date, datetime
import os
from typing import Callable, List, Optional, Tuple

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, find_local_file, load_cached_or_fetch, recent_cache_ttl, refresh_cached_if_stale
from ..discover import DiscoveryReport
from ..http import probe_url_variants
from ..model import (
    MONTH_ABBR,
    MONTH_NAMES,
    OperatorDetailTuple,
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_month_result,
    log_skip,
    resolve_fetch_months,
)
from ..parse import XLSX_MAGIC, clean_lower, find_sheet_fuzzy, is_number, is_xlsx_bytes, open_xlsx_workbook

OhioDetailRow = OperatorDetailTuple
OhioFiscalDetailRow = Tuple[
    str, float, float, Optional[float], Optional[float], Optional[float], Optional[float],
    Optional[float], Optional[float], Optional[float],
]
OhioMonthCore = Tuple[float, float, float, float, float, float, List[OhioDetailRow]]
OhioMonthFiscal = Tuple[float, Optional[float], List[OhioFiscalDetailRow]]

STATE_NAME = "Ohio"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

_DEFAULT_LOG = build_state_logger("OH")


_OH_REPORTS_BASE = "https://dam.assets.ohio.gov/raw/upload/casinocontrol.ohio.gov/revenue-reports"


def candidate_workbook_urls(year: int, latest_report_number: int) -> list[str]:
    """Return cumulative-XLSX URLs to try, newest naming first."""
    urls = [
        f"{_OH_REPORTS_BASE}/{year}/Sports/{year}_Sports_Gaming_Revenue_Report{report_number:02d}.xlsx"
        for report_number in range(latest_report_number, 0, -1)
    ]
    urls.extend((
        f"{_OH_REPORTS_BASE}/{year}/Sports/{year}_Sports_Gaming_Revenue_Report.xlsx",
        f"{_OH_REPORTS_BASE}/{year}/{year}_Sports_Gaming_Revenue_Report.xlsx",
        f"{_OH_REPORTS_BASE}/Full%20Year%20Revenue%20Reports/December%20{year}%20Ohio%20Sports%20Gaming%20Revenue%20Report.xlsx",
    ))
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
        signature_check=XLSX_MAGIC,
        wayback_timestamp=f"{year}12",
        log=log,
        report=report,
        report_key=f"{year}:cumulative",
        matched_url=matched_url,
    )


def _extract_subtotal_pair(row: tuple[object, ...]) -> tuple[float, float, list[float]] | None:
    numeric = [float(cell) for cell in row if is_number(cell)]
    if len(row) > 5 and is_number(row[1]) and is_number(row[5]):
        return float(row[1]), float(row[5]), numeric
    if len(numeric) >= 5:
        return numeric[0], numeric[4], numeric
    return None


def _parse_month_sheet_with_fiscal(
    sheet,
) -> tuple[OhioMonthCore, float, float | None, list[OhioFiscalDetailRow]] | None:
    online_handle = None
    online_revenue = None
    retail_handle = None
    retail_revenue = None
    promo_col = 4
    details_fiscal: list[OhioFiscalDetailRow] = []
    section = None

    def _append_operator_row(op_label: str, row_vals: tuple[object, ...], sec: str) -> None:
        handle = float(row_vals[1])
        revenue_value = float(row_vals[5])
        promo_val = float(row_vals[promo_col]) if len(row_vals) > promo_col and is_number(row_vals[promo_col]) else None
        rh = handle if sec == "retail" else None
        rr = revenue_value if sec == "retail" else None
        mh = handle if sec == "online" else None
        mr = revenue_value if sec == "online" else None
        details_fiscal.append((op_label, handle, revenue_value, rh, rr, mh, mr, revenue_value, promo_val, None))

    for row in sheet.iter_rows(values_only=True):
        if not any(cell is not None for cell in row):
            continue
        label = str((row[0] if row else None) or "").strip()
        lower_label = label.lower()
        if (
            section
            and label
            and not any(kw in lower_label for kw in ("subtotal", "total", "proprietor", "type "))
            and len(row) > 5
            and is_number(row[1])
            and is_number(row[5])
        ):
            _append_operator_row(label, row, section)
            continue
        row_text = " ".join(str(cell or "") for cell in row).lower()
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
                if "promotional" in clean_lower(cell):
                    promo_col = idx
                    break
            continue
        if "subtotal type a online" in row_text:
            pair = _extract_subtotal_pair(row)
            if pair is not None:
                online_handle, online_revenue, _ = pair
            section = None
        elif "subtotal type b retail" in row_text:
            pair = _extract_subtotal_pair(row)
            if pair is not None:
                retail_handle, retail_revenue, _ = pair
            section = None
        elif "total online and retail" in row_text:
            pair = _extract_subtotal_pair(row)
            if pair is None:
                continue
            total_handle, total_revenue, numeric = pair
            total_promo = (
                float(row[promo_col])
                if len(row) > promo_col and is_number(row[promo_col])
                else (numeric[3] if len(numeric) >= 6 else None)
            )
            if None not in (online_handle, online_revenue, retail_handle, retail_revenue):
                details = [entry[:7] for entry in details_fiscal]
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
        elif (
            section
            and label
            and not any(kw in lower_label for kw in ("subtotal", "total", "proprietor", "type "))
            and len(row) > 5
            and is_number(row[1])
            and is_number(row[5])
        ):
            _append_operator_row(label, row, section)
    return None


def _parse_cumulative_xlsx_with_fiscal(
    data: bytes,
    target_months: list[tuple[date, str, str]],
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[dict[str, OhioMonthCore], dict[str, OhioMonthFiscal]]:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Ohio workbooks")
    with open_xlsx_workbook(data, openpyxl_mod=openpyxl, log=log) as workbook:
        if workbook is None:
            return {}, {}
        results: dict[str, OhioMonthCore] = {}
        fiscal_results: dict[str, OhioMonthFiscal] = {}
        for _mdate, month_name, label in target_months:
            sheet = find_sheet_fuzzy(workbook, month_name)
            if sheet is None:
                continue
            parsed = _parse_month_sheet_with_fiscal(sheet)
            if parsed is not None:
                core, total_gross, total_promo, details_fiscal = parsed
                results[label] = core
                fiscal_results[label] = (total_gross, total_promo, details_fiscal)
        return results, fiscal_results


def _parse_local_files_with_fiscal(
    local_dir: str,
    year: int,
    target_months: list[tuple[date, str, str]],
    log: Callable[[str], None] = _DEFAULT_LOG,
):
    matched = find_local_file(
        local_dir,
        lambda entry: entry.lower().endswith(".xlsx") and str(year) in entry,
        reverse=True,
    )
    if matched is not None and is_xlsx_bytes(matched[1]):
        log(f"INFO using local workbook {matched[0]}")
        return _parse_cumulative_xlsx_with_fiscal(matched[1], target_months, log=log)
    return {}, {}


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
        (mdate, MONTH_NAMES[mdate.month], f"{MONTH_ABBR[mdate.month]} {mdate.year}")
        for mdate in target_dates
    ]
    parsed_by_label: dict[str, OhioMonthCore] = {}
    fiscal_by_label: dict[str, OhioMonthFiscal] = {}
    source_by_year: dict[int, str] = {}
    for year in sorted({mdate.year for mdate, _, _ in months}):
        try:
            year_months = [entry for entry in months if entry[0].year == year]
            latest_requested = max(mdate for mdate, _, _ in year_months)
            latest_report_number = latest_requested.month
            recent_ttl = recent_cache_ttl(latest_requested.year, latest_requested.month)
            found_urls: list[str] = []
            fetch_called = [False]

            def _download_oh(
                y: int = year,
                lrn: int = latest_report_number,
                urls: list[str] = found_urls,
                called: list[bool] = fetch_called,
            ) -> bytes | None:
                called[0] = True
                return download_cumulative_workbook(y, lrn, report=report, log=logger, matched_url=urls)

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
                    validate=is_xlsx_bytes,
                    cache_ttl_seconds=recent_ttl,
                    offline=offline,
                )
                if data:
                    parsed, parsed_fiscal = _parse_cumulative_xlsx_with_fiscal(data, year_months, log=logger)
                if (
                    data is not None
                    and was_cached
                    and not fetch_called[0]
                    and not offline
                    and path
                    and len(parsed) < len(year_months)
                    and recent_ttl is not None
                ):
                    refreshed = refresh_cached_if_stale(
                        path,
                        data,
                        fetcher=_download_oh,
                        validate=is_xlsx_bytes,
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
    for mdate, _, label in months:
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
        operator_details = build_operator_results(iso_month, details_fiscal)
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
        log_month_result(logger, label, item)
    return results
