"""Mississippi sports wagering regulatory fetcher and parser (MSGC)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, find_local_file, load_cached_or_fetch, recent_cache_ttl
from ..discover import DiscoveryReport
from ..http import probe_url_variants
from ..model import (
    StateMonthResult,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_month_result,
    log_skip,
    resolve_fetch_months,
)
from ..parse import ensure_xlsx, is_number, open_xlsx_workbook

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

URL_VARIANT_TEMPLATES = [
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xlsx",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xls",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_Sports_Wagering.xlsx",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_Sports_Wagering.xls",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_sports_wagering.xlsx_.xls",
    "https://www.msgamingcommission.com/files/monthly_reports/{mm}{yy}_Sports_Wagering.xlsx_.xls",
]
BASE_URL = URL_VARIANT_TEMPLATES[0]
REGION_ORDER = ["Central", "Coastal", "Northern"]

_DEFAULT_LOG = build_state_logger("MS")


def download_xlsx(
    mm: str,
    yy: str,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    matched_url: list[str] | None = None,
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
        matched_url=matched_url,
    )


def parse_state_totals(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, list[tuple[str, float, float]]] | None:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Mississippi workbooks")
    converted = ensure_xlsx(data, log=log)
    with open_xlsx_workbook(converted, openpyxl_mod=openpyxl, log=log) as workbook:
        if workbook is None:
            return None
        state_totals: tuple[float, float] | None = None
        region_totals: list[tuple[float, float]] = []
        detail_rows: list[tuple[str, float, float]] = []
        for sheet in workbook.worksheets:
            region_name = None
            for row in sheet.iter_rows(values_only=True):
                label = str(row[0] if row else "").strip().lower()
                if region_name is None and label in {"central", "coastal", "northern"}:
                    region_name = str(row[0]).strip()
                if "overall for state" in label or "overall for region" in label:
                    handle = row[2] if len(row) > 2 else None
                    revenue = row[4] if len(row) > 4 else None
                    if not (is_number(handle) and is_number(revenue)):
                        continue
                    pair = (float(handle), float(revenue))
                    if "overall for state" in label and state_totals is None:
                        state_totals = pair
                    elif "overall for region" in label:
                        region_totals.append(pair)
                        if region_name:
                            detail_rows.append((region_name, *pair))
        if state_totals is not None:
            return state_totals[0], state_totals[1], detail_rows
        if region_totals:
            return (
                sum(handle for handle, _ in region_totals),
                sum(revenue for _, revenue in region_totals),
                detail_rows,
            )
        return None


def parse_local_file(
    local_dir: str,
    mm: str,
    yy: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, list[tuple[str, float, float]]] | None:
    prefix = f"{mm}{yy}".lower()
    matched = find_local_file(
        local_dir,
        lambda entry: entry.lower().startswith(prefix)
        and entry.lower().endswith((".xlsx", ".xls", ".xlsx_.xls")),
    )
    return parse_state_totals(matched[1], log=log) if matched is not None else None


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
            found_urls: list[str] = []
            if parsed is None:
                path = cache_path(cache_dir, f"mississippi_{yyyy}_{mm}.xlsx")
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda m=mm, y=yy, urls=found_urls: download_xlsx(
                        m, y, report=report, log=logger, matched_url=urls,
                    ),
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {label}",
                    fetch_error_message=f"could not download Mississippi workbook for {label}",
                    validate=lambda payload: payload.startswith((b"PK", b"\xd0\xcf\x11\xe0")),
                    cache_ttl_seconds=recent_cache_ttl(mdate.year, mdate.month),
                    offline=offline,
                )
                if data:
                    parsed = parse_state_totals(data, log=logger)
            if parsed is None:
                log_skip(logger, label, "workbook unavailable or unparseable")
                continue
            handle, revenue, regions = parsed
            region_map = {
                region_name: (region_handle, region_revenue)
                for region_name, region_handle, region_revenue in regions
            }
            extra: dict[str, float | None] = {}
            for region_name in REGION_ORDER:
                region_handle, region_revenue = region_map.get(region_name, (None, None))
                extra[f"{region_name} Handle"] = region_handle
                extra[f"{region_name} Revenue"] = region_revenue
            item = finalize_state_month(
                state=STATE_NAME,
                iso_month=iso_month,
                handle=handle,
                revenue=revenue,
                retail_handle=handle,
                retail_revenue=revenue,
                mobile_handle=None,
                mobile_revenue=None,
                source_url=found_urls[-1] if found_urls else source_url,
                reconcile_splits=False,
                extra=extra,
                log=logger,
                label=label,
            )
            if item is None:
                continue
            results.append(item)
            log_month_result(logger, label, item, include_splits=False)
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
