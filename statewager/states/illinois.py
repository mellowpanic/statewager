"""Illinois sports wagering regulatory fetcher and parser (IGB)."""
from __future__ import annotations

import csv
from datetime import date, datetime
import os
import re
from typing import Callable
from urllib.parse import urljoin

import requests

from ..cache import cache_age_seconds, cache_path, load_cache, recent_cache_ttl, try_save_cache
from ..discover import DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_with_wayback_fallback, probe_url_variants, retry_request
from ..model import (
    MONTH_ABBR,
    MONTH_NAMES,
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_month_result,
    log_skip,
    month_yyyymm,
    resolve_fetch_months,
    sum_optional,
)
from ..parse import PDF_MAGIC, clean_lower, collapse_ws, extract_pdf_text, is_pdf_bytes, parse_dollar

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


def _legacy_template_vars(month_name: str, year: str) -> dict[str, str]:
    return {
        "month": month_name,
        "year": year,
        "month_lower": month_name.lower(),
        "abbr": month_name[:3],
    }


def _legacy_pdf_url(month_name: str, year: str) -> str:
    return LEGACY_PDF_TEMPLATES[0].format(**_legacy_template_vars(month_name, year))


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
    timestamp = month_yyyymm(f"{month_name} {year}")
    label = f"{month_name[:3]} {year}"
    resolved_urls = dict(discover_igb_pdf_urls(log=log)) if discovered_urls is None else discovered_urls
    discovered_url = resolved_urls.get(label)
    if discovered_url:
        data = fetch_with_wayback_fallback(
            discovered_url, timestamp_yyyymm=timestamp, signature_check=PDF_MAGIC, log=log,
        )
        if report is not None:
            report.add_probe(
                f"{label}:pdf-discovered",
                discovered_url,
                status="ok" if data else "fail",
                reason="scraped-link" if data else "fetch-failed",
            )
        if data:
            if matched_url is not None:
                matched_url[:] = [discovered_url]
            log(f"INFO downloaded PDF via scraped link for {label}: {discovered_url}")
            return data
    return probe_url_variants(
        LEGACY_PDF_TEMPLATES,
        _legacy_template_vars(month_name, year),
        signature_check=PDF_MAGIC,
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


def _first_amount_over(line: str, threshold: float, *, use_abs: bool = False) -> float | None:
    for token in _NUM_TOKEN_RE.findall(line):
        amount = parse_dollar(token)
        if amount is not None and (abs(amount) if use_abs else amount) > threshold:
            return amount
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
            handle = _first_amount_over(line, 1_000_000)
        if agr is None and ("adjusted gross receipt" in lower or "agr" in lower):
            agr = _first_amount_over(line, 100_000, use_abs=True)
    if handle is not None and agr is not None:
        return handle, agr
    log("WARN Illinois PDF parse returned incomplete totals")
    return None


def classify_location_type(value: object) -> str | None:
    text = clean_lower(value)
    if text == "in-person wagering":
        return "retail"
    if text == "online wagering":
        return "mobile"
    return None


def _add_licensee_channel(
    by_licensee: dict[str, dict[str, float | bool]],
    licensee: str,
    location_type: str,
    metric_key: str,
    value: float,
) -> None:
    rec = by_licensee.setdefault(
        licensee,
        {"rh": 0.0, "mh": 0.0, "rr": 0.0, "mr": 0.0, "has_retail": False, "has_mobile": False},
    )
    rec[metric_key] = float(rec[metric_key]) + value
    rec[f"has_{location_type}"] = True


def _scan_cash_csv(
    data: bytes,
) -> tuple[
    tuple[float, float, float] | None,
    dict[str, dict[str, float | bool]],
    bool,
]:
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    header_index = next((i for i, line in enumerate(lines) if "Sport Level" in line and "Tier 1 Handle" in line), None)
    if header_index is None:
        return None, {}, False
    retail_handle = 0.0
    mobile_handle = 0.0
    by_licensee: dict[str, dict[str, float | bool]] = {}
    reader = csv.DictReader(lines[header_index:])
    if reader.fieldnames:
        reader.fieldnames = [name.strip() if name else "" for name in reader.fieldnames]
    for row in reader:
        location_type = classify_location_type(row.get("Location Type"))
        tier1 = parse_dollar(row.get("Tier 1 Handle"))
        tier2 = parse_dollar(row.get("Tier 2 Handle"))
        if (tier1 is None and tier2 is None) or location_type is None:
            continue
        handle = (tier1 or 0.0) + (tier2 or 0.0)
        if location_type == "retail":
            retail_handle += handle
            key = "rh"
        else:
            mobile_handle += handle
            key = "mh"
        licensee = collapse_ws(row.get("Licensee"))
        if licensee:
            _add_licensee_channel(by_licensee, licensee, location_type, key, handle)
    total_handle = retail_handle + mobile_handle
    totals = (total_handle, retail_handle, mobile_handle) if total_handle > 0 else None
    return totals, by_licensee, True


def _scan_accrual_csv(
    data: bytes,
) -> tuple[
    tuple[float, float, float] | None,
    float | None,
    dict[str, dict[str, float | bool]],
    dict[str, float],
    bool,
    bool,
    int,
]:
    lines = data.decode("utf-8-sig", errors="replace").splitlines()
    header_index = next((i for i, line in enumerate(lines) if "Location Type" in line and "State AGR" in line), None)
    if header_index is None:
        return None, None, {}, {}, False, False, 0
    reader = csv.DictReader(lines[header_index:])
    if reader.fieldnames:
        reader.fieldnames = [name.strip() if name else "" for name in reader.fieldnames]
    tax_cols = ("State Tax", "Cook County Tax", "Sports Wager Tax")
    fieldnames = {name for name in (reader.fieldnames or []) if name}
    has_tax_cols = any(col in fieldnames for col in tax_cols)

    retail_agr = 0.0
    mobile_agr = 0.0
    agr_rows = 0
    by_licensee: dict[str, dict[str, float | bool]] = {}
    total_tax = 0.0
    tax_rows = 0
    op_taxes: dict[str, float] = {}

    for row in reader:
        location_type = classify_location_type(row.get("Location Type"))
        if location_type is None:
            continue
        licensee = collapse_ws(row.get("Licensee"))
        state_agr = parse_dollar(row.get("State AGR"))
        if state_agr is not None:
            agr_rows += 1
            if location_type == "retail":
                retail_agr += state_agr
                key = "rr"
            else:
                mobile_agr += state_agr
                key = "mr"
            if licensee:
                _add_licensee_channel(by_licensee, licensee, location_type, key, state_agr)
        if has_tax_cols:
            row_tax = sum_optional(*(parse_dollar(row.get(col)) for col in tax_cols))
            if row_tax is not None:
                total_tax += row_tax
                tax_rows += 1
                if licensee:
                    op_taxes[licensee] = op_taxes.get(licensee, 0.0) + row_tax

    agr_totals = (retail_agr + mobile_agr, retail_agr, mobile_agr) if agr_rows > 0 else None
    rounded_tax = round(total_tax, 2) if (has_tax_cols and tax_rows > 0) else None
    rounded_op_taxes = {name: round(val, 2) for name, val in op_taxes.items()} if rounded_tax is not None else {}
    return agr_totals, rounded_tax, by_licensee, rounded_op_taxes, True, has_tax_cols, tax_rows


def _build_details_from_scans(
    cash_ops: dict[str, dict[str, float | bool]],
    accrual_ops: dict[str, dict[str, float | bool]],
) -> list[tuple[str, float, float, float | None, float | None, float | None, float | None]]:
    all_licensees: dict[str, None] = {name: None for name in cash_ops}
    for name in accrual_ops:
        all_licensees.setdefault(name, None)
    details: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
    for licensee in all_licensees:
        c_rec = cash_ops.get(licensee)
        a_rec = accrual_ops.get(licensee)
        has_retail = bool((c_rec and c_rec["has_retail"]) or (a_rec and a_rec["has_retail"]))
        has_mobile = bool((c_rec and c_rec["has_mobile"]) or (a_rec and a_rec["has_mobile"]))
        rh = float(c_rec["rh"]) if (has_retail and c_rec) else (0.0 if has_retail else None)
        rr = float(a_rec["rr"]) if (has_retail and a_rec) else (0.0 if has_retail else None)
        mh = float(c_rec["mh"]) if (has_mobile and c_rec) else (0.0 if has_mobile else None)
        mr = float(a_rec["mr"]) if (has_mobile and a_rec) else (0.0 if has_mobile else None)
        total_h = (rh or 0.0) + (mh or 0.0)
        total_r = (rr or 0.0) + (mr or 0.0)
        if total_h == 0.0 and total_r == 0.0:
            continue
        details.append((licensee, total_h, total_r, rh, rr, mh, mr))
    return details


def parse_accrual_csv(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[float, float, float] | None:
    totals, _tax, _ops, _op_taxes, found_header, _has_tax, _tax_rows = _scan_accrual_csv(data)
    if not found_header:
        log("WARN Illinois accrual CSV header row not found")
        return None
    if totals is None:
        log("WARN Illinois accrual CSV parse produced zero AGR rows")
    return totals


def parse_accrual_tax(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float | None, dict[str, float]]:
    """Extract State + Cook County + Sports Wager Tax from an Illinois accrual CSV."""
    _a_tot, total_tax, _ops, op_taxes, found_header, has_tax_cols, tax_rows = _scan_accrual_csv(data)
    if not found_header or not has_tax_cols:
        return None, {}
    if tax_rows == 0:
        log("WARN Illinois accrual CSV parse produced zero tax rows")
        return None, {}
    return total_tax, op_taxes


def _local_target_tokens(month_name: str, year: str) -> set[str]:
    return {
        f"{month_name}{year}".lower(),
        f"{month_name[:3]}{year}".lower(),
    }


def _scan_local_month_files(
    local_dir: str,
    month_name: str,
    year: str,
) -> tuple[bytes | None, bytes | None, list[str]]:
    normalized_targets = _local_target_tokens(month_name, year)
    cash_data: bytes | None = None
    accrual_data: bytes | None = None
    pdf_paths: list[str] = []
    if not os.path.isdir(local_dir):
        return None, None, []
    for entry in sorted(os.listdir(local_dir)):
        lower = entry.replace(" ", "").lower()
        if not any(target in lower for target in normalized_targets):
            continue
        path = os.path.join(local_dir, entry)
        if not os.path.isfile(path):
            continue
        if lower.endswith(".csv"):
            if "detail" in lower:
                with open(path, "rb") as file_handle:
                    cash_data = file_handle.read()
            elif "tax" in lower:
                with open(path, "rb") as file_handle:
                    accrual_data = file_handle.read()
        elif lower.endswith(".pdf"):
            pdf_paths.append(path)
    return cash_data, accrual_data, pdf_paths


def _assemble_csv_pair(
    cash_scan: tuple[tuple[float, float, float] | None, dict[str, dict[str, float | bool]], bool] | None,
    accrual_scan: tuple[
        tuple[float, float, float] | None,
        float | None,
        dict[str, dict[str, float | bool]],
        dict[str, float],
        bool,
        bool,
        int,
    ] | None,
    label: str,
    log: Callable[[str], None],
) -> tuple[
    tuple[float, float, float, float, float, float],
    float | None,
    list[tuple[object, ...]],
] | None:
    if cash_scan is None or accrual_scan is None:
        return None
    cash_totals, cash_ops, c_found = cash_scan
    if not c_found:
        log("WARN Illinois cash CSV header row not found")
    elif cash_totals is None:
        log("WARN Illinois cash CSV parse produced no handle totals")
    agr_totals, state_tax, accrual_ops, op_taxes, a_found, has_tax_cols, tax_rows = accrual_scan
    if not a_found:
        log("WARN Illinois accrual CSV header row not found")
    elif agr_totals is None:
        log("WARN Illinois accrual CSV parse produced zero AGR rows")
    if cash_totals is None or agr_totals is None:
        log(f"WARN {label}: CSV parse returned cash={bool(cash_totals)} accrual={bool(agr_totals)}")
        return None
    if a_found and has_tax_cols and tax_rows == 0:
        log("WARN Illinois accrual CSV parse produced zero tax rows")
    detail_tuples = [
        (*op_tuple, None, None, op_taxes.get(op_tuple[0]))
        for op_tuple in _build_details_from_scans(cash_ops, accrual_ops)
    ]
    total_handle, retail_handle, mobile_handle = cash_totals
    total_revenue, retail_revenue, mobile_revenue = agr_totals
    return (
        (total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue),
        state_tax,
        detail_tuples,
    )


def _parse_csv_pair_full(
    cash_csv: bytes | None,
    accrual_csv: bytes | None,
    label: str,
    log: Callable[[str], None],
) -> tuple[
    tuple[float, float, float, float, float, float],
    float | None,
    list[tuple[object, ...]],
] | None:
    if not (cash_csv and accrual_csv):
        return None
    return _assemble_csv_pair(_scan_cash_csv(cash_csv), _scan_accrual_csv(accrual_csv), label, log)


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
    aem_bootstrap_attempts = 0

    def _fetch_aem_pair(
        month_name: str,
        year: str,
        label: str,
        *,
        cash_csv: bytes | None,
        accrual_csv: bytes | None,
        cash_path: str | None,
        accrual_path: str | None,
    ) -> tuple[bytes | None, bytes | None]:
        nonlocal aem_session, aem_tokens, aem_bootstrap_attempts
        if (not cash_csv or not accrual_csv) and aem_tokens is None and aem_bootstrap_attempts < 2:
            if aem_session is not None:
                aem_session.close()
            aem_session = requests.Session()
            aem_tokens = _bootstrap_aem_tokens(aem_session, log=logger)
            aem_bootstrap_attempts += 1
        for stype, cur, cpath, kind_label, scanner in (
            ("TypeCash", cash_csv, cash_path, "detail", _scan_cash_csv),
            ("TypeAccrual", accrual_csv, accrual_path, "tax", _scan_accrual_csv),
        ):
            if cur:
                continue
            fetched = (
                post_report_csv(month_name, year, stype, log=logger, session=aem_session, tokens=aem_tokens)
                if aem_tokens
                else None
            )
            if fetched:
                if scanner(fetched)[0] is None:
                    logger(f"WARN fetched Illinois {kind_label} CSV for {label} failed validation; ignoring")
                    fetched = None
                else:
                    try_save_cache(cpath, fetched, log=logger)
            else:
                logger(f"WARN could not fetch Illinois {kind_label} CSV for {label}")
            if stype == "TypeCash":
                cash_csv = fetched
            else:
                accrual_csv = fetched
        return cash_csv, accrual_csv

    def _fetch_legacy_pdf(
        month_name: str,
        year: str,
        label: str,
        *,
        legacy_path: str | None,
    ) -> tuple[tuple[float, float, None, None, None, None], str] | None:
        nonlocal discovered_pdf_urls
        matched: list[str] = []
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
        if not data:
            logger(f"WARN could not download Illinois legacy PDF for {label}")
            return None
        try_save_cache(legacy_path, data, log=logger)
        pdf_totals = parse_pdf(data, log=logger)
        if pdf_totals is None:
            logger(f"WARN {label}: legacy PDF did not yield totals")
            return None
        return (pdf_totals[0], pdf_totals[1], None, None, None, None), (matched[0] if matched else _legacy_pdf_url(month_name, year))

    try:
        for mdate in target_dates:
            month_name = MONTH_NAMES[mdate.month]
            year = f"{mdate.year}"
            month_num = f"{mdate.month:02d}"
            label = f"{MONTH_ABBR[mdate.month]} {mdate.year}"
            iso_month = format_iso_month(mdate.year, mdate.month)
            try:
                logger(f"INFO processing {label}")
                parsed_tuple = None
                source_url: str | None = None
                state_tax: float | None = None
                detail_tuples: list[tuple[object, ...]] = []

                def _unpack_csv(csv_full) -> bool:
                    nonlocal parsed_tuple, state_tax, detail_tuples, source_url
                    if csv_full is None:
                        return False
                    parsed_tuple, state_tax, detail_tuples = csv_full
                    source_url = REPORT_URL
                    return True

                if local_dir:
                    local_cash, local_accrual, local_pdfs = _scan_local_month_files(local_dir, month_name, year)
                    if _unpack_csv(_parse_csv_pair_full(local_cash, local_accrual, label, logger)):
                        logger(f"INFO parsed local export for {label}")
                    else:
                        for pdf_path in local_pdfs:
                            with open(pdf_path, "rb") as file_handle:
                                pdf_res = parse_pdf(file_handle.read(), log=logger)
                            if pdf_res is not None:
                                parsed_tuple = (pdf_res[0], pdf_res[1], None, None, None, None)
                                source_url = REPORT_URL
                                logger(f"INFO parsed local export for {label}")
                                break
                cached_cash = None
                cached_accrual = None
                stale_cash_scan = None
                stale_accrual_scan = None
                cash_scan = None
                accrual_scan = None
                cash_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_detail.csv")
                accrual_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_tax.csv")
                ttl_seconds = recent_cache_ttl(mdate.year, mdate.month)

                def _load_csv_cache(path: str | None, scanner, kind_label: str):
                    payload = load_cache(path)
                    if not payload:
                        return None, None, None
                    scan = scanner(payload)
                    if scan[0] is None:
                        logger(f"WARN invalid cached {kind_label} CSV for {label}; ignoring")
                        return None, None, None
                    if not offline and ttl_seconds is not None:
                        age = cache_age_seconds(path)
                        if age is not None and age > ttl_seconds:
                            return None, None, scan
                    logger(f"INFO using cached {kind_label} CSV for {label}")
                    return payload, scan, None

                def _resolve_scan(scan, stale_scan, fresh_bytes, scanner):
                    if scan is not None:
                        return scan
                    if not fresh_bytes and stale_scan is not None:
                        return stale_scan
                    return scanner(fresh_bytes) if fresh_bytes else None

                if parsed_tuple is None:
                    cached_cash, cash_scan, stale_cash_scan = _load_csv_cache(
                        cash_path, _scan_cash_csv, "detail",
                    )
                    cached_accrual, accrual_scan, stale_accrual_scan = _load_csv_cache(
                        accrual_path, _scan_accrual_csv, "tax",
                    )
                    _unpack_csv(_assemble_csv_pair(cash_scan, accrual_scan, label, logger))
                cached_pdf = None
                legacy_path = cache_path(cache_dir, f"illinois_{year}_{month_num}_legacy.pdf")
                if parsed_tuple is None:
                    cached_pdf = load_cache(legacy_path)
                    if cached_pdf and not is_pdf_bytes(cached_pdf):
                        logger(f"WARN invalid cached legacy PDF for {label}; ignoring")
                        cached_pdf = None
                    elif cached_pdf:
                        logger(f"INFO using cached legacy PDF for {label}")
                        pdf_totals = parse_pdf(cached_pdf, log=logger)
                        if pdf_totals is not None:
                            parsed_tuple = (pdf_totals[0], pdf_totals[1], None, None, None, None)
                            source_url = _legacy_pdf_url(month_name, year)
                if parsed_tuple is None and not offline:
                    if mdate.year <= 2024 and not cached_pdf:
                        pdf_res = _fetch_legacy_pdf(month_name, year, label, legacy_path=legacy_path)
                        if pdf_res is not None:
                            parsed_tuple, source_url = pdf_res
                    if parsed_tuple is None:
                        cash_csv, accrual_csv = _fetch_aem_pair(
                            month_name,
                            year,
                            label,
                            cash_csv=cached_cash,
                            accrual_csv=cached_accrual,
                            cash_path=cash_path,
                            accrual_path=accrual_path,
                        )
                        final_cash_scan = _resolve_scan(cash_scan, stale_cash_scan, cash_csv, _scan_cash_csv)
                        final_accrual_scan = _resolve_scan(
                            accrual_scan, stale_accrual_scan, accrual_csv, _scan_accrual_csv,
                        )
                        _unpack_csv(_assemble_csv_pair(final_cash_scan, final_accrual_scan, label, logger))
                if parsed_tuple is None:
                    log_skip(logger, label, "no parseable local export, legacy PDF, or CSV form result")
                    continue
                handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue = parsed_tuple
                operator_details = build_operator_results(iso_month, detail_tuples) if detail_tuples else []
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
                log_month_result(logger, label, item)
            except Exception as exc:
                logger(f"WARN failed {label}: {exc}")
    finally:
        if aem_session is not None:
            aem_session.close()
    return results
