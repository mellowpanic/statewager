"""Illinois sports wagering regulatory fetcher and parser (IGB)."""
from __future__ import annotations

import csv
from datetime import date, datetime
import io
import os
import re
from typing import Callable
from urllib.parse import urljoin

import requests

from ..cache import cache_path, load_cache, save_cache
from ..discover import DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_with_wayback_fallback, probe_url_variants, retry_request
from ..model import (
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
)
from ..parse import extract_pdf_text, parse_dollar

STATE_NAME = "Illinois"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

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
_VIEWSTATE_RE = re.compile(r'name="__VIEWSTATE"[^>]*value="([^"]+)"')
_VIEWSTATEGEN_RE = re.compile(r'name="__VIEWSTATEGENERATOR"[^>]*value="([^"]+)"')
_EVENTVAL_RE = re.compile(r'name="__EVENTVALIDATION"[^>]*value="([^"]+)"')
_NUM_TOKEN_RE = re.compile(r"[+\-\u2212]?\(?\$?[\d,]+\.?\d*\)?")
_DEFAULT_LOG = build_state_logger("IL")


def discover_igb_pdf_urls(
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[tuple[str, str], ...]:
    """Scrape the IGB sports reports page for direct PDF download links."""
    links = crawl_anchors(IGB_REPORTS_INDEX, log=log)
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
    *,
    discovered_urls: dict[str, str] | None = None,
    matched_url: list[str] | None = None,
) -> bytes | None:
    timestamp: str | None = None
    try:
        timestamp = datetime.strptime(f"{month_name} {year}", "%B %Y").strftime("%Y%m")
    except ValueError:
        pass
    label = datetime.strptime(f"{month_name} {year}", "%B %Y").strftime("%b %Y")
    resolved_urls = dict(discover_igb_pdf_urls(log=log)) if discovered_urls is None else discovered_urls
    discovered_url = resolved_urls.get(label)
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
            if matched_url is not None:
                matched_url[:] = [discovered_url]
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
        matched_url=matched_url,
    )


def _bootstrap_aem_tokens(
    session: requests.Session,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[str, str, str] | None:
    try:
        response = retry_request(
            "GET",
            REPORT_URL,
            retries=HTTP_RETRIES,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            session=session,
        )
    except Exception as exc:
        log(f"WARN Illinois form bootstrap failed: {exc}")
        return None
    html = response.text
    viewstate = _VIEWSTATE_RE.search(html)
    viewstate_generator = _VIEWSTATEGEN_RE.search(html)
    event_validation = _EVENTVAL_RE.search(html)
    if not viewstate or not viewstate_generator or not event_validation:
        log("WARN Illinois form tokens were not present")
        return None
    return viewstate.group(1), viewstate_generator.group(1), event_validation.group(1)


def post_report_csv(
    month_name: str,
    year: str,
    search_type: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    session: requests.Session | None = None,
    tokens: tuple[str, str, str] | None = None,
) -> bytes | None:
    owns_session = session is None
    active_session = session or requests.Session()
    try:
        active_tokens = tokens or _bootstrap_aem_tokens(active_session, log=log)
        if active_tokens is None:
            return None
        vs, vs_gen, ev_val = active_tokens
        form = {
            "__VIEWSTATE": vs,
            "__VIEWSTATEGENERATOR": vs_gen,
            "__EVENTVALIDATION": ev_val,
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
                session=active_session,
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
    finally:
        if owns_session:
            active_session.close()


def parse_pdf(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[float, float] | None:
    text = extract_pdf_text(data, log=log)
    if not text:
        return None
    handle = None
    agr = None
    for line in text.splitlines():
        lower = line.lower()
        if handle is None and "total handle" in lower:
            for token in _NUM_TOKEN_RE.findall(line):
                amount = parse_dollar(token)
                if amount is not None and amount > 1_000_000:
                    handle = amount
                    break
        if agr is None and ("adjusted gross receipt" in lower or "adjusted gross receipts" in lower or "agr" in lower):
            for token in _NUM_TOKEN_RE.findall(line):
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
    matched_rows = 0
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    for row in reader:
        location_type = classify_location_type(row.get("Location Type"))
        state_agr = parse_dollar(row.get("State AGR"))
        if state_agr is None:
            continue
        if location_type == "retail":
            retail_agr += state_agr
            matched_rows += 1
        elif location_type == "mobile":
            mobile_agr += state_agr
            matched_rows += 1
    if matched_rows == 0:
        log("WARN Illinois accrual CSV parse produced zero AGR rows")
        return None
    return (retail_agr + mobile_agr, retail_agr, mobile_agr)


def parse_csv_details(
    cash_data: bytes,
    accrual_data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> list[tuple[str, float, float, float | None, float | None, float | None, float | None]]:
    """Extract per-licensee retail/mobile handle and State AGR from Illinois CSV exports."""
    cash_lines = cash_data.decode("utf-8-sig", errors="replace").splitlines()
    cash_hdr = next((i for i, line in enumerate(cash_lines) if "Sport Level" in line and "Tier 1 Handle" in line), None)
    accrual_lines = accrual_data.decode("utf-8-sig", errors="replace").splitlines()
    accrual_hdr = next((i for i, line in enumerate(accrual_lines) if "Location Type" in line and "State AGR" in line), None)
    if cash_hdr is None or accrual_hdr is None:
        return []

    by_licensee: dict[str, dict[str, float | bool]] = {}

    def _entry(licensee: str) -> dict[str, float | bool]:
        return by_licensee.setdefault(
            licensee,
            {
                "rh": 0.0,
                "rr": 0.0,
                "mh": 0.0,
                "mr": 0.0,
                "has_retail": False,
                "has_mobile": False,
            },
        )

    for row in csv.DictReader(io.StringIO("\n".join(cash_lines[cash_hdr:]))):
        licensee = " ".join(str(row.get("Licensee") or "").split())
        location_type = classify_location_type(row.get("Location Type"))
        if not licensee or location_type is None:
            continue
        tier1 = parse_dollar(row.get("Tier 1 Handle"))
        tier2 = parse_dollar(row.get("Tier 2 Handle"))
        if tier1 is None and tier2 is None:
            continue
        amount = (tier1 or 0.0) + (tier2 or 0.0)
        rec = _entry(licensee)
        if location_type == "retail":
            rec["rh"] = float(rec["rh"]) + amount
            rec["has_retail"] = True
        else:
            rec["mh"] = float(rec["mh"]) + amount
            rec["has_mobile"] = True

    for row in csv.DictReader(io.StringIO("\n".join(accrual_lines[accrual_hdr:]))):
        licensee = " ".join(str(row.get("Licensee") or "").split())
        location_type = classify_location_type(row.get("Location Type"))
        state_agr = parse_dollar(row.get("State AGR"))
        if not licensee or location_type is None or state_agr is None:
            continue
        rec = _entry(licensee)
        if location_type == "retail":
            rec["rr"] = float(rec["rr"]) + state_agr
            rec["has_retail"] = True
        else:
            rec["mr"] = float(rec["mr"]) + state_agr
            rec["has_mobile"] = True

    details: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
    for licensee, rec in by_licensee.items():
        rh = float(rec["rh"]) if rec["has_retail"] else None
        rr = float(rec["rr"]) if rec["has_retail"] else None
        mh = float(rec["mh"]) if rec["has_mobile"] else None
        mr = float(rec["mr"]) if rec["has_mobile"] else None
        total_h = (rh or 0.0) + (mh or 0.0)
        total_r = (rr or 0.0) + (mr or 0.0)
        if total_h == 0.0 and total_r == 0.0:
            continue
        details.append((licensee, total_h, total_r, rh, rr, mh, mr))
    return details


def parse_accrual_tax(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float | None, dict[str, float]]:
    """Extract State + Cook County + Sports Wager Tax from an Illinois accrual CSV."""
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    header_index = next((i for i, line in enumerate(lines) if "Location Type" in line and "State AGR" in line), None)
    if header_index is None:
        return None, {}
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_index:])))
    tax_cols = ("State Tax", "Cook County Tax", "Sports Wager Tax")
    fieldnames = {name.strip() for name in (reader.fieldnames or []) if name}
    if not any(col in fieldnames for col in tax_cols):
        return None, {}
    total_tax = 0.0
    matched_rows = 0
    by_licensee: dict[str, float] = {}
    for row in reader:
        location_type = classify_location_type(row.get("Location Type"))
        if location_type is None:
            continue
        parsed_parts = [parse_dollar(row.get(col)) for col in tax_cols]
        if all(val is None for val in parsed_parts):
            continue
        row_tax = sum(val for val in parsed_parts if val is not None)
        total_tax += row_tax
        matched_rows += 1
        licensee = " ".join(str(row.get("Licensee") or "").split())
        if licensee:
            by_licensee[licensee] = by_licensee.get(licensee, 0.0) + row_tax
    if matched_rows == 0:
        log("WARN Illinois accrual CSV parse produced zero tax rows")
        return None, {}
    return round(total_tax, 2), {name: round(val, 2) for name, val in by_licensee.items()}


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
    pdf_paths: list[str] = []
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
            pdf_paths.append(path)
    if cash_data and accrual_data:
        cash = parse_cash_csv(cash_data, log=log)
        accrual = parse_accrual_csv(accrual_data, log=log)
        if cash is not None and accrual is not None:
            total_handle, retail_handle, mobile_handle = cash
            total_revenue, retail_revenue, mobile_revenue = accrual
            return total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue
    for path in pdf_paths:
        with open(path, "rb") as file_handle:
            parsed = parse_pdf(file_handle.read(), log=log)
        if parsed is not None:
            total_handle, total_revenue = parsed
            return total_handle, total_revenue, None, None, None, None
    return None


def _read_local_csv_pair(local_dir: str, month_name: str, year: str) -> tuple[bytes | None, bytes | None]:
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
        if not lower.endswith(".csv") or not any(target in lower for target in normalized_targets):
            continue
        path = os.path.join(local_dir, entry)
        if "detail" in lower:
            with open(path, "rb") as file_handle:
                cash_data = file_handle.read()
        elif "tax" in lower:
            with open(path, "rb") as file_handle:
                accrual_data = file_handle.read()
    return cash_data, accrual_data
_ORIG_DOWNLOAD_PDF = download_pdf
_ORIG_POST_REPORT_CSV = post_report_csv


def _try_parse_csv_pair(
    cash_csv: bytes | None,
    accrual_csv: bytes | None,
    label: str,
    log: Callable[[str], None],
) -> tuple[float, float, float, float, float, float] | None:
    if not (cash_csv and accrual_csv):
        return None
    cash = parse_cash_csv(cash_csv, log=log)
    accrual = parse_accrual_csv(accrual_csv, log=log)
    if cash is None or accrual is None:
        log(f"WARN {label}: CSV parse returned cash={bool(cash)} accrual={bool(accrual)}")
        return None
    total_handle, retail_handle, mobile_handle = cash
    total_revenue, retail_revenue, mobile_revenue = accrual
    return (
        total_handle,
        total_revenue,
        retail_handle,
        retail_revenue,
        mobile_handle,
        mobile_revenue,
    )


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
    discovered_pdf_urls: dict[str, str] | None = None
    aem_session: requests.Session | None = None
    aem_tokens: tuple[str, str, str] | None = None
    aem_bootstrapped = False

    def _fetch_aem_pair(
        month_name: str,
        year: str,
        month_num: str,
        label: str,
        *,
        cash_csv: bytes | None = None,
        accrual_csv: bytes | None = None,
        cash_path: str | None = None,
        accrual_path: str | None = None,
    ) -> tuple[bytes | None, bytes | None]:
        nonlocal aem_session, aem_tokens, aem_bootstrapped
        if cash_path is None:
            cash_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_detail.csv")
        if accrual_path is None:
            accrual_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_tax.csv")
        if offline:
            return cash_csv, accrual_csv
        if (not cash_csv or not accrual_csv) and post_report_csv is _ORIG_POST_REPORT_CSV and not aem_bootstrapped:
            aem_session = requests.Session()
            aem_tokens = _bootstrap_aem_tokens(aem_session, log=logger)
            aem_bootstrapped = True
        if not cash_csv:
            if post_report_csv is _ORIG_POST_REPORT_CSV:
                cash_csv = post_report_csv(month_name, year, "TypeCash", log=logger, session=aem_session, tokens=aem_tokens) if aem_tokens else None
            else:
                cash_csv = post_report_csv(month_name, year, "TypeCash", log=logger)
            if cash_csv:
                save_cache(cash_path, cash_csv)
            else:
                logger(f"WARN could not fetch Illinois detail CSV for {label}")
        if not accrual_csv:
            if post_report_csv is _ORIG_POST_REPORT_CSV:
                accrual_csv = post_report_csv(month_name, year, "TypeAccrual", log=logger, session=aem_session, tokens=aem_tokens) if aem_tokens else None
            else:
                accrual_csv = post_report_csv(month_name, year, "TypeAccrual", log=logger)
            if accrual_csv:
                save_cache(accrual_path, accrual_csv)
            else:
                logger(f"WARN could not fetch Illinois tax CSV for {label}")
        return cash_csv, accrual_csv

    def _fetch_legacy_pdf(
        month_name: str,
        year: str,
        month_num: str,
        label: str,
        *,
        legacy_path: str | None = None,
    ) -> tuple[tuple[float, float, None, None, None, None], str] | None:
        nonlocal discovered_pdf_urls
        if legacy_path is None:
            legacy_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_legacy.pdf")
        matched: list[str] = []
        if offline:
            return None
        if download_pdf is _ORIG_DOWNLOAD_PDF:
            if discovered_pdf_urls is None:
                discovered_pdf_urls = dict(discover_igb_pdf_urls(log=logger))
            data = download_pdf(
                month_name,
                year,
                report=report,
                log=logger,
                discovered_urls=discovered_pdf_urls,
                matched_url=matched,
            )
        else:
            data = download_pdf(month_name, year, report=report, log=logger)
        if data:
            save_cache(legacy_path, data)
        else:
            logger(f"WARN could not download Illinois legacy PDF for {label}")
            return None
        pdf_totals = parse_pdf(data, log=logger)
        if pdf_totals is None:
            logger(f"WARN {label}: legacy PDF did not yield totals")
            return None
        fallback_url = LEGACY_PDF_TEMPLATES[0].format(
            month=month_name,
            year=year,
            month_lower=month_name.lower(),
            abbr=month_name[:3],
        )
        return (pdf_totals[0], pdf_totals[1], None, None, None, None), (matched[0] if matched else fallback_url)

    try:
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
                raw_csv_pair: tuple[bytes, bytes] | None = None
                if local_dir:
                    parsed_tuple = parse_local_file(local_dir, month_name, year, log=logger)
                    if parsed_tuple:
                        logger(f"INFO parsed local export for {label}")
                        source_url = REPORT_URL
                        local_cash, local_accrual = _read_local_csv_pair(local_dir, month_name, year)
                        if local_cash and local_accrual:
                            raw_csv_pair = (local_cash, local_accrual)
                # 1. Check cached CSVs first without making any network calls.
                cached_cash = None
                cached_accrual = None
                cash_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_detail.csv")
                accrual_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_tax.csv")
                if parsed_tuple is None:
                    cached_cash = load_cache(cash_path)
                    if cached_cash:
                        logger(f"INFO using cached detail CSV for {label}")
                    cached_accrual = load_cache(accrual_path)
                    if cached_accrual:
                        logger(f"INFO using cached tax CSV for {label}")
                    if cached_cash and cached_accrual:
                        parsed_tuple = _try_parse_csv_pair(cached_cash, cached_accrual, label, logger)
                        if parsed_tuple is not None:
                            source_url = REPORT_URL
                            raw_csv_pair = (cached_cash, cached_accrual)
                # 2. Check cached legacy PDF without making any network calls.
                cached_pdf = None
                legacy_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_legacy.pdf")
                if parsed_tuple is None:
                    cached_pdf = load_cache(legacy_path)
                    if cached_pdf:
                        logger(f"INFO using cached legacy PDF for {label}")
                        pdf_totals = parse_pdf(cached_pdf, log=logger)
                        if pdf_totals is not None:
                            parsed_tuple = (pdf_totals[0], pdf_totals[1], None, None, None, None)
                            source_url = LEGACY_PDF_TEMPLATES[0].format(
                                month=month_name,
                                year=year,
                                month_lower=month_name.lower(),
                                abbr=month_name[:3],
                            )
                # 3. Remote fetch (legacy PDF only for <= 2024; AEM CSV for >= 2025 or fallback).
                if parsed_tuple is None and not offline:
                    if mdate.year <= 2024 and cached_pdf is None:
                        pdf_res = _fetch_legacy_pdf(
                            month_name, year, month_num, label, legacy_path=legacy_path,
                        )
                        if pdf_res is not None:
                            parsed_tuple, source_url = pdf_res
                    if parsed_tuple is None and not (cached_cash and cached_accrual):
                        cash_csv, accrual_csv = _fetch_aem_pair(
                            month_name,
                            year,
                            month_num,
                            label,
                            cash_csv=cached_cash,
                            accrual_csv=cached_accrual,
                            cash_path=cash_path,
                            accrual_path=accrual_path,
                        )
                        parsed_tuple = _try_parse_csv_pair(cash_csv, accrual_csv, label, logger)
                        if parsed_tuple is not None:
                            source_url = REPORT_URL
                            if cash_csv and accrual_csv:
                                raw_csv_pair = (cash_csv, accrual_csv)
                if parsed_tuple is None:
                    log_skip(logger, label, "no parseable local export, legacy PDF, or CSV form result")
                    continue
                handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue = parsed_tuple
                state_tax: float | None = None
                operator_details = []
                if raw_csv_pair is not None:
                    state_tax, op_taxes = parse_accrual_tax(raw_csv_pair[1], log=logger)
                    detail_tuples = [
                        (*op_tuple, None, None, op_taxes.get(op_tuple[0]))
                        for op_tuple in parse_csv_details(raw_csv_pair[0], raw_csv_pair[1], log=logger)
                    ]
                    operator_details = build_operator_results(iso_month, detail_tuples)
                item = finalize_state_month(
                    state=STATE_NAME,
                    iso_month=iso_month,
                    handle=handle,
                    revenue=revenue,
                    retail_handle=retail_handle,
                    retail_revenue=retail_revenue,
                    mobile_handle=mobile_handle,
                    mobile_revenue=mobile_revenue,
                    tax=state_tax,
                    source_url=source_url,
                    details=operator_details,
                    log=logger,
                    label=label,
                )
                if item is None:
                    continue
                results.append(item)
                logger(
                    f"INFO {label}: handle={item.handle:,.0f} revenue={item.revenue:,.0f} "
                    f"retail_handle={item.retail_handle if item.retail_handle is not None else 'n/a'} "
                    f"mobile_handle={item.mobile_handle if item.mobile_handle is not None else 'n/a'} hold={item.hold:.1f}%"
                )
            except Exception as exc:
                logger(f"WARN failed {label}: {exc}")
    finally:
        if aem_session is not None:
            aem_session.close()
    return results
