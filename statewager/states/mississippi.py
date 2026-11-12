"""Mississippi sports wagering regulatory fetcher and parser (MSGC)."""
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
    StateMonthResult,
    build_state_logger,
    format_iso_month,
    hold_pct,
    log_skip,
    resolve_fetch_months,
    validate_result,
)
from ..parse import ensure_xlsx, is_number

STATE_NAME = "Mississippi"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = [
    "Central Handle",
    "Central Revenue",
    "Coastal Handle",
    "Coastal Revenue",
    "Northern Handle",
    "Northern Revenue",
]
HAS_DETAILS = False

USER_AGENT = "Mozilla/5.0"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
BASE_URL = "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xlsx"
URL_VARIANT_TEMPLATES = [
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xlsx",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xls",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_Sports_Wagering.xlsx",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_Sports_Wagering.xls",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xlsx_.xls",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_Sports_Wagering.xlsx_.xls",
]
REGION_ORDER = ["Central", "Coastal", "Northern"]

_DEFAULT_LOG = build_state_logger("MS")


def download_xlsx(
    mm: str,
    yy: str,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    """Try the known MSGC filename variants and return XLSX bytes."""
    return probe_url_variants(
        URL_VARIANT_TEMPLATES,
        {"mm": mm, "yy": yy},
        signature_check=None,
        wayback_timestamp=f"20{yy}{mm}",
        convert_xls=True,
        log=log,
        report=report,
        report_key=f"20{yy}-{mm}",
    )


def parse_state_totals(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, list[tuple[str, float, float]]] | None:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Mississippi workbooks")
    converted = ensure_xlsx(data, log=log)
    if converted is None:
        return None
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(converted), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open workbook: {exc}")
        return None
    try:
        region_totals: list[tuple[float, float]] = []
        detail_rows: list[tuple[str, float, float]] = []
        for sheet in workbook.worksheets:
            region_name = None
            for row in sheet.iter_rows(values_only=True):
                label = str(row[0] if row else "").strip().lower()
                if region_name is None and label in {"central", "coastal", "northern"}:
                    region_name = str(row[0]).strip()
                if "overall for state" in label:
                    handle = row[2] if len(row) > 2 else None
                    revenue = row[4] if len(row) > 4 else None
                    if is_number(handle) and is_number(revenue):
                        return float(handle), float(revenue), detail_rows
                if "overall for region" in label:
                    handle = row[2] if len(row) > 2 else None
                    revenue = row[4] if len(row) > 4 else None
                    if is_number(handle) and is_number(revenue):
                        region_totals.append((float(handle), float(revenue)))
                        if region_name:
                            detail_rows.append((region_name, float(handle), float(revenue)))
        if region_totals:
            return (
                sum(handle for handle, _ in region_totals),
                sum(revenue for _, revenue in region_totals),
                detail_rows,
            )
        return None
    finally:
        workbook.close()


def parse_local_file(
    local_dir: str,
    mm: str,
    yy: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, list[tuple[str, float, float]]] | None:
    prefix = f"{mm}{yy}".lower()
    for entry in sorted(os.listdir(local_dir)):
        lower = entry.lower()
        if not lower.startswith(prefix):
            continue
        if not (lower.endswith(".xlsx") or lower.endswith(".xls") or lower.endswith(".xlsx_.xls")):
            continue
        with open(os.path.join(local_dir, entry), "rb") as file_handle:
            return parse_state_totals(file_handle.read(), log=log)
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
    """Fetch and parse Mississippi monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Mississippi sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    current_year_2digit = date.today().strftime("%y")
    results: list[StateMonthResult] = []
    for mdate in target_dates:
        mm = mdate.strftime("%m")
        yy = mdate.strftime("%y")
        yyyy = mdate.strftime("%Y")
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(mdate.year, mdate.month)
        source_url = BASE_URL.format(mm=mm, yy=yy)
        try:
            logger(f"INFO processing {label}")
            parsed = parse_local_file(local_dir, mm, yy, log=logger) if local_dir else None
            if parsed:
                logger(f"INFO parsed local workbook for {label}")
            if parsed is None:
                path = cache_path(cache_dir, f"mississippi_{yyyy}_{mm}.xlsx")
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda m=mm, y=yy: download_xlsx(m, y, report=report, log=logger),
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {label}",
                    fetch_error_message=f"could not download Mississippi workbook for {label}",
                    validate=lambda payload: payload.startswith(b"PK"),
                    cache_ttl_seconds=7 * 86400 if yy >= current_year_2digit else None,
                    offline=offline,
                )
                if data:
                    parsed = parse_state_totals(data, log=logger)
            if parsed is None:
                log_skip(logger, label, "workbook unavailable or unparseable")
                continue
            handle, revenue, regions = parsed
            validated = validate_result(label, handle, revenue)
            if validated is None:
                continue
            handle, revenue = validated
            hold = hold_pct(handle, revenue)
            retail_hold = hold_pct(handle, revenue) if handle else None
            region_map = {
                region_name: (region_handle, region_revenue)
                for region_name, region_handle, region_revenue in regions
            }
            extra: dict[str, float | None] = {}
            for region_name in REGION_ORDER:
                region_handle, region_revenue = region_map.get(region_name, (None, None))
                extra[f"{region_name} Handle"] = region_handle
                extra[f"{region_name} Revenue"] = region_revenue
            results.append(
                StateMonthResult(
                    state=STATE_NAME,
                    month=iso_month,
                    handle=handle,
                    revenue=revenue,
                    hold=hold,
                    retail_handle=handle,
                    retail_revenue=revenue,
                    retail_hold=retail_hold,
                    mobile_handle=None,
                    mobile_revenue=None,
                    mobile_hold=None,
                    source_url=source_url,
                    extra=extra,
                )
            )
            logger(f"INFO {label}: handle={handle:,.0f} revenue={revenue:,.0f} hold={hold:.1f}%")
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
