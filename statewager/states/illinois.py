"""Illinois sports wagering regulatory fetcher and parser (IGB)."""
from __future__ import annotations

import csv
from datetime import date, datetime
import functools
import io
import os
import re
from typing import Callable
from urllib.parse import urljoin

import requests

from ..cache import cache_path, load_cached_or_fetch
from ..discover import DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_with_wayback_fallback, probe_url_variants, retry_request
from ..model import (
    StateMonthResult,
    build_state_logger,
    format_iso_month,
    hold_pct,
    log_skip,
    reconcile_components,
    resolve_fetch_months,
    validate_result,
)
from ..parse import extract_pdf_text, parse_dollar

STATE_NAME = "Illinois"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = False

USER_AGENT = "Mozilla/5.0"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
REPORT_URL = "https://igbapps.illinois.gov/SportsReports_AEM.aspx"
IGB_REPORTS_INDEX = "https://igb.illinois.gov/sports-wagering/sports-reports.html"
LEGACY_PDF_TEMPLATES = [
    "https://www.igb.illinois.gov/SportsReports/{month}{year}SWReport.pdf",
    "https://igb.illinois.gov/SportsReports/{month}{year}SWReport.pdf",
    "https://www.igb.illinois.gov/SportsReports/{month}_{year}_SWReport.pdf",
    "https://www.igb.illinois.gov/SportsReports/{month}{year}_SWReport.pdf",
    "https://www.igb.illinois.gov/SportsReports/{month_lower}{year}SWReport.pdf",
    "https://igb.illinois.gov/SportsReports/{month_lower}{year}swreport.pdf",
    "https://www.igb.illinois.gov/SportsReports/{abbr}{year}SWReport.pdf",
]
_DEFAULT_LOG = build_state_logger("IL")


@functools.cache
def discover_igb_pdf_urls() -> tuple[tuple[str, str], ...]:
    """Scrape the IGB sports reports page for direct PDF download links."""
    links = crawl_anchors(IGB_REPORTS_INDEX, log=_DEFAULT_LOG)
    if not links:
        return ()
    found: dict[str, str] = {}
    for link in links:
        href = link.get("href", "").strip()
        if not href.lower().endswith(".pdf"):
            continue
        combined = " ".join([href, link.get("title", ""), link.get("text", "")])
        if "sw" not in combined.lower() and "sports" not in combined.lower():
            continue
        label = extract_month_label(combined)
        if label:
            found.setdefault(label, urljoin(IGB_REPORTS_INDEX, href))
    return tuple(found.items())


def download_pdf(
    month_name: str,
    year: str,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    timestamp: str | None = None
    try:
        timestamp = datetime.strptime(f"{month_name} {year}", "%B %Y").strftime("%Y%m")
    except ValueError:
        pass
    label = datetime.strptime(f"{month_name} {year}", "%B %Y").strftime("%b %Y")
    discovered_urls = dict(discover_igb_pdf_urls())
    discovered_url = discovered_urls.get(label)
    if discovered_url:
        data = fetch_with_wayback_fallback(
            discovered_url, timestamp_yyyymm=timestamp, signature_check=b"%PDF", log=log,
        )
        if report is not None:
            report.add_probe(
                f"{label}:pdf-discovered",
                discovered_url,
                status="ok" if data else "fail",
                reason="scraped-link" if data else "fetch-failed",
            )
        if data and data.startswith(b"%PDF"):
            log(f"INFO downloaded PDF via scraped link for {label}: {discovered_url}")
            return data
    abbr = month_name[:3]
    return probe_url_variants(
        LEGACY_PDF_TEMPLATES,
        {"month": month_name, "year": year, "month_lower": month_name.lower(), "abbr": abbr},
        signature_check=b"%PDF",
        wayback_timestamp=timestamp,
        log=log,
        report=report,
        report_key=f"{label}:pdf",
    )


def post_report_csv(
    month_name: str,
    year: str,
    search_type: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    headers = {"User-Agent": USER_AGENT}
    session = requests.Session()
    try:
        response = retry_request(
            "GET",
            REPORT_URL,
            retries=HTTP_RETRIES,
            timeout=HTTP_TIMEOUT,
            headers=headers,
            session=session,
        )
    except Exception as exc:
        log(f"WARN Illinois form bootstrap failed for {month_name} {year} {search_type}: {exc}")
        return None
    html = response.text
    viewstate = re.search(r'name="__VIEWSTATE"[^>]*value="([^"]+)"', html)
    viewstate_generator = re.search(r'name="__VIEWSTATEGENERATOR"[^>]*value="([^"]+)"', html)
    event_validation = re.search(r'name="__EVENTVALIDATION"[^>]*value="([^"]+)"', html)
    if not viewstate or not viewstate_generator or not event_validation:
        log(f"WARN Illinois form tokens were not present for {month_name} {year} {search_type}")
        return None
    form = {
        "__VIEWSTATE": viewstate.group(1),
        "__VIEWSTATEGENERATOR": viewstate_generator.group(1),
        "__EVENTVALIDATION": event_validation.group(1),
        "SearchType": search_type,
        "SearchCash": "Detail Report",
        "SearchAccrual": "Tax Summary Report",
        "SearchStartMonth": month_name,
        "SearchStartYear": year,
        "SearchEndMonth": month_name,
        "SearchEndYear": year,
        "ViewType": "ViewCSV",
        "ButtonSearch.x": "48",
        "ButtonSearch.y": "14",
    }
    try:
        response = retry_request(
            "POST",
            REPORT_URL,
            retries=HTTP_RETRIES,
            timeout=HTTP_TIMEOUT,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": REPORT_URL,
            },
            session=session,
            data=form,
        )
    except Exception as exc:
        log(f"WARN Illinois form POST failed for {month_name} {year} {search_type}: {exc}")
        return None
    data = response.content
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/csv" in content_type:
        return data
    text = data.decode("utf-8", errors="replace")
    if "returned no revenue results" in text.lower():
        log(f"WARN Illinois returned no results for {month_name} {year} {search_type}")
        return None
    log(f"WARN Illinois returned unexpected content for {month_name} {year} {search_type}")
    return None


def parse_pdf(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[float, float] | None:
    text = extract_pdf_text(data, log=log)
    if not text:
        return None
    handle = None
    agr = None
    for line in text.splitlines():
        lower = line.lower()
        if handle is None and "total handle" in lower:
            for token in re.findall(r"[\($-]?[\d,]+\.?\d*\)?", line):
                amount = parse_dollar(token)
                if amount is not None and amount > 1_000_000:
                    handle = amount
                    break
        if agr is None and ("adjusted gross receipt" in lower or "adjusted gross receipts" in lower or "agr" in lower):
            for token in re.findall(r"[\($-]?[\d,]+\.?\d*\)?", line):
                amount = parse_dollar(token)
                if amount is not None and abs(amount) > 100_000:
                    agr = amount
                    break
    if handle is not None and agr is not None:
        return handle, agr
    log("WARN Illinois PDF parse returned incomplete totals")
    return None


def classify_location_type(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if text == "in-person wagering":
        return "retail"
    if text == "online wagering":
        return "mobile"
    return None


def parse_cash_csv(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[float, float, float] | None:
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    header_index = next((i for i, line in enumerate(lines) if "Sport Level" in line and "Tier 1 Handle" in line), None)
    if header_index is None:
        log("WARN Illinois cash CSV header row not found")
        return None
    retail_handle = 0.0
    mobile_handle = 0.0
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    for row in reader:
        location_type = classify_location_type(row.get("Location Type"))
        tier1 = parse_dollar(row.get("Tier 1 Handle"))
        tier2 = parse_dollar(row.get("Tier 2 Handle"))
        if tier1 is None and tier2 is None:
            continue
        handle = (tier1 or 0.0) + (tier2 or 0.0)
        if location_type == "retail":
            retail_handle += handle
        elif location_type == "mobile":
            mobile_handle += handle
    total_handle = retail_handle + mobile_handle
    if total_handle <= 0:
        log("WARN Illinois cash CSV parse produced no handle totals")
    return (total_handle, retail_handle, mobile_handle) if total_handle > 0 else None


def parse_accrual_csv(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[float, float, float] | None:
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    header_index = next((i for i, line in enumerate(lines) if "Location Type" in line and "State AGR" in line), None)
    if header_index is None:
        log("WARN Illinois accrual CSV header row not found")
        return None
    retail_agr = 0.0
    mobile_agr = 0.0
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    for row in reader:
        location_type = classify_location_type(row.get("Location Type"))
        state_agr = parse_dollar(row.get("State AGR"))
        if state_agr is None:
            continue
        if location_type == "retail":
            retail_agr += state_agr
        elif location_type == "mobile":
            mobile_agr += state_agr
    total_agr = retail_agr + mobile_agr
    if total_agr == 0:
        log("WARN Illinois accrual CSV parse produced zero AGR totals")
    return (total_agr, retail_agr, mobile_agr) if total_agr != 0 else None


def parse_local_file(
    local_dir: str,
    month_name: str,
    year: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None] | None:
    normalized_targets = {
        f"{month_name}{year}swreport".lower(),
        f"{month_name[:3]}{year}swreport".lower(),
        f"{month_name}{year}".lower(),
        f"{month_name[:3]}{year}".lower(),
    }
    cash_data = None
    accrual_data = None
    for entry in sorted(os.listdir(local_dir)):
        lower = entry.replace(" ", "").lower()
        if not any(target in lower for target in normalized_targets):
            continue
        path = os.path.join(local_dir, entry)
        if lower.endswith(".csv"):
            if "detail" in lower:
                with open(path, "rb") as file_handle:
                    cash_data = file_handle.read()
            elif "tax" in lower:
                with open(path, "rb") as file_handle:
                    accrual_data = file_handle.read()
        elif lower.endswith(".pdf"):
            with open(path, "rb") as file_handle:
                parsed = parse_pdf(file_handle.read(), log=log)
            if parsed is not None:
                total_handle, total_revenue = parsed
                return total_handle, total_revenue, None, None, None, None
    if cash_data and accrual_data:
        cash = parse_cash_csv(cash_data, log=log)
        accrual = parse_accrual_csv(accrual_data, log=log)
        if cash is not None and accrual is not None:
            total_handle, retail_handle, mobile_handle = cash
            total_revenue, retail_revenue, mobile_revenue = accrual
            return total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue
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
    """Fetch and parse Illinois monthly sports wagering results."""
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    results: list[StateMonthResult] = []
    for mdate in target_dates:
        month_name = mdate.strftime("%B")
        year = mdate.strftime("%Y")
        month_num = mdate.strftime("%m")
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(mdate.year, mdate.month)
        try:
            logger(f"INFO processing {label}")
            parsed_tuple = None
            source_url: str | None = None
            if local_dir:
                parsed_tuple = parse_local_file(local_dir, month_name, year, log=logger)
                if parsed_tuple:
                    logger(f"INFO parsed local export for {label}")
                    source_url = REPORT_URL
            if parsed_tuple is None:
                legacy_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_legacy.pdf")
                data = load_cached_or_fetch(
                    cache_file=legacy_path,
                    fetcher=lambda mn=month_name, yr=year: download_pdf(mn, yr, report=report, log=logger),
                    log=logger,
                    cache_hit_message=f"INFO using cached legacy PDF for {label}",
                    fetch_error_message=f"could not download Illinois legacy PDF for {label}",
                    offline=offline,
                )
                if data:
                    pdf_totals = parse_pdf(data, log=logger)
                    if pdf_totals is None:
                        logger(f"WARN {label}: legacy PDF did not yield totals")
                    else:
                        parsed_tuple = (pdf_totals[0], pdf_totals[1], None, None, None, None)
                        source_url = LEGACY_PDF_TEMPLATES[0].format(
                            month=month_name,
                            year=year,
                            month_lower=month_name.lower(),
                            abbr=month_name[:3],
                        )
            if parsed_tuple is None:
                cash_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_detail.csv")
                accrual_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_tax.csv")
                cash_csv = load_cached_or_fetch(
                    cache_file=cash_path,
                    fetcher=lambda mn=month_name, yr=year: post_report_csv(mn, yr, "TypeCash", log=logger),
                    log=logger,
                    cache_hit_message=f"INFO using cached detail CSV for {label}",
                    fetch_error_message=f"could not fetch Illinois detail CSV for {label}",
                    offline=offline,
                )
                accrual_csv = load_cached_or_fetch(
                    cache_file=accrual_path,
                    fetcher=lambda mn=month_name, yr=year: post_report_csv(mn, yr, "TypeAccrual", log=logger),
                    log=logger,
                    cache_hit_message=f"INFO using cached tax CSV for {label}",
                    fetch_error_message=f"could not fetch Illinois tax CSV for {label}",
                    offline=offline,
                )
                if cash_csv and accrual_csv:
                    cash = parse_cash_csv(cash_csv, log=logger)
                    accrual = parse_accrual_csv(accrual_csv, log=logger)
                    if cash is None or accrual is None:
                        logger(f"WARN {label}: CSV parse returned cash={bool(cash)} accrual={bool(accrual)}")
                    else:
                        total_handle, retail_handle, mobile_handle = cash
                        total_revenue, retail_revenue, mobile_revenue = accrual
                        parsed_tuple = (
                            total_handle,
                            total_revenue,
                            retail_handle,
                            retail_revenue,
                            mobile_handle,
                            mobile_revenue,
                        )
                        source_url = REPORT_URL
            if parsed_tuple is None:
                log_skip(logger, label, "no parseable local export, legacy PDF, or CSV form result")
                continue
            handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue = parsed_tuple
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
                )
            )
            logger(
                f"INFO {label}: handle={handle:,.0f} revenue={revenue:,.0f} "
                f"retail_handle={retail_handle if retail_handle is not None else 'n/a'} "
                f"mobile_handle={mobile_handle if mobile_handle is not None else 'n/a'} hold={hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
