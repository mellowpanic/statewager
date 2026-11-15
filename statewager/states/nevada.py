"""Nevada sports wagering regulatory fetcher and parser (NGCB GRI)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import os
import re
import subprocess
import tempfile
from typing import Callable
from urllib.parse import urljoin

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, load_cache, save_cache
from ..discover import MONTH_NAMES as CORE_MONTH_NAMES, AnchorParser, DiscoveryReport
from ..http import _XLS_SIGNATURES, fetch_bytes
from ..model import (
    StateMonthResult,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
)
from ..parse import _which, ensure_xlsx, parse_dollar

STATE_NAME = "Nevada"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = [
    "Clark Handle",
    "Clark Revenue",
    "Washoe Handle",
    "Washoe Revenue",
    "Other Handle",
    "Other Revenue",
]
HAS_DETAILS = False

BASE = "https://gaming.nv.gov"
USER_AGENT = "Mozilla/5.0"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
GRI_PAGE_URL = f"{BASE}/about-us/gaming-revenue-information-gri/"
REGION_ORDER = ["Clark County", "Washoe County", "Other Areas"]
REGION_EXTRA_KEYS = {
    "Clark County": ("Clark Handle", "Clark Revenue"),
    "Washoe County": ("Washoe Handle", "Washoe Revenue"),
    "Other Areas": ("Other Handle", "Other Revenue"),
}
PDFPLUMBER_MIN_WORDS = 100
PDFPLUMBER_SAMPLE_PAGES = 3
NGCB_PDFTOTEXT_PAGE_HINTS = {
    "Statewide": 4,
    "Clark County": 9,
    "Washoe County": 34,
}
AREA_LABELS = {
    "Statewide": "Statewide - All Nonrestricted Locations",
    "Clark County": "Clark County - All Nonrestricted Locations",
    "Washoe County": "Washoe County - All Nonrestricted Locations",
}

MONTH_NAMES = {**CORE_MONTH_NAMES, "sept": 9}
MONTH_RE = "|".join(sorted(MONTH_NAMES.keys(), key=len, reverse=True))
MONTH_YEAR_PATTERNS = [
    re.compile(rf"\b({MONTH_RE})\b[\s_\-/(),]+(20\d{{2}})\b", re.IGNORECASE),
    re.compile(rf"\b(20\d{{2}})[\s_\-/(),]+({MONTH_RE})\b", re.IGNORECASE),
]
_DIGIT_ALPHA_SPLIT_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", re.IGNORECASE)
_COMPACT_YM_RE = re.compile(rf"\b(20\d{{2}})({MONTH_RE})\b", re.IGNORECASE)
_COMPACT_MY_RE = re.compile(rf"\b({MONTH_RE})(20\d{{2}})\b", re.IGNORECASE)
_SPORTS_LINE_RE = re.compile(r"(?:\d+\s+){0,3}([()\d,]+)\s+([()\d.\-]+)\s+([()\d.\-]+)")

_DEFAULT_LOG = build_state_logger("NV")


def fetch_nv_bytes(url: str, retries: int = HTTP_RETRIES, timeout: int = HTTP_TIMEOUT) -> bytes | None:
    """Fetch NGCB content with browser-like headers; return None on 404."""
    return fetch_bytes(
        url,
        retries=retries,
        timeout=timeout,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Referer": GRI_PAGE_URL,
        },
    )


def extract_month_year(value: str) -> tuple[int, int] | None:
    text = unescape(value or "").replace("%20", " ")
    text = _DIGIT_ALPHA_SPLIT_RE.sub(" ", text)
    for pattern in MONTH_YEAR_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        first, second = match.groups()
        if first.isdigit():
            year = int(first)
            month = MONTH_NAMES[second.lower()]
        else:
            month = MONTH_NAMES[first.lower()]
            year = int(second)
        return month, year
    compact = _COMPACT_YM_RE.search(text)
    if compact:
        return MONTH_NAMES[compact.group(2).lower()], int(compact.group(1))
    compact_rev = _COMPACT_MY_RE.search(text)
    if compact_rev:
        return MONTH_NAMES[compact_rev.group(1).lower()], int(compact_rev.group(2))
    return None


def discover_report_urls(log: Callable[[str], None] = _DEFAULT_LOG) -> dict[tuple[int, int], dict[str, str]]:
    log("INFO scraping NGCB GRI landing page for report links...")
    try:
        html_bytes = fetch_nv_bytes(GRI_PAGE_URL)
    except Exception as exc:
        log(f"WARN could not fetch GRI page: {exc}")
        return {}
    if not html_bytes:
        return {}
    html = html_bytes.decode("utf-8", errors="replace")
    parser = AnchorParser()
    parser.feed(html)
    urls: dict[tuple[int, int], dict[str, str]] = {}
    for link in parser.links:
        href = unescape(link.get("href", "").strip())
        if not href:
            continue
        lower = href.lower()
        if not lower.endswith((".xlsx", ".xls", ".pdf")):
            continue
        match_date = None
        for value in (href, link.get("title", ""), link.get("text", "")):
            match_date = extract_month_year(value)
            if match_date:
                break
        if not match_date:
            continue
        month, year = match_date
        extension = ".xlsx" if lower.endswith(".xlsx") else ".xls" if lower.endswith(".xls") else ".pdf"
        label = datetime(year, month, 1).strftime("%b %Y")
        absolute_url = urljoin(BASE, href)
        current = urls.setdefault((month, year), {})
        if extension in (".xlsx", ".xls"):
            current["excel"] = absolute_url
        elif "excel" not in current:
            current["pdf"] = absolute_url
        log(f"INFO discovered {label} {extension} -> {absolute_url}")
    log(f"INFO discovered report links for {len(urls)} months")
    return urls


def build_pdf_url_candidates(month: int, year: int) -> list[str]:
    month_date = date(year, month, 1)
    month_name = month_date.strftime("%B").lower()
    month_short = month_date.strftime("%b")
    lower_abbr = month_short.lower()
    base_path = f"{BASE}/siteassets/content/about/gaming-revenue"
    uploaded_path = f"{BASE}/uploadedFiles/gamingnvgov/content/about/gaming-revenue"
    return [
        f"{base_path}/monthly-revenue-report----{month_name}-{year}.pdf",
        f"{base_path}/monthly-revenue-report---{month_name}-{year}.pdf",
        f"{base_path}/monthly-revenue-report--{month_name}-{year}.pdf",
        f"{base_path}/monthly-revenue-report-{month_name}-{year}.pdf",
        f"{uploaded_path}/{year}{month_short}-gri.pdf",
        f"{uploaded_path}/{year % 100:02d}{month_short}-gri.pdf",
        f"{base_path}/{year}{lower_abbr}-gri.pdf",
    ]


def download_pdf(month: int, year: int, log: Callable[[str], None] = _DEFAULT_LOG) -> bytes | None:
    for url in build_pdf_url_candidates(month, year):
        try:
            data = fetch_nv_bytes(url)
        except Exception as exc:
            log(f"WARN could not download {url}: {exc}")
            continue
        if data and data.startswith(b"%PDF"):
            log(f"INFO downloaded {url}")
            return data
    return None


def download_report(
    month: int,
    year: int,
    discovered_urls: dict[tuple[int, int], dict[str, str]],
    cache_dir: str | None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> list[tuple[str, bytes]]:
    discovered = discovered_urls.get((month, year), {})
    downloads: list[tuple[str, bytes]] = []
    for kind in ("excel", "pdf"):
        url = discovered.get(kind)
        if not url:
            continue
        try:
            data = fetch_nv_bytes(url)
        except Exception as exc:
            log(f"WARN could not download {url}: {exc}")
            continue
        if data:
            valid = data.startswith(_XLS_SIGNATURES) if kind == "excel" else data.startswith(b"%PDF")
            if not valid:
                log(f"WARN discarding unexpected {kind} payload from {url}")
                continue
            log(f"INFO downloaded {url}")
            extension = "xlsx" if kind == "excel" else "pdf"
            save_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.{extension}"), data)
            downloads.append((kind, data))
    if not any(kind == "pdf" for kind, _ in downloads):
        pdf_data = download_pdf(month, year, log=log)
        if pdf_data:
            save_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.pdf"), pdf_data)
            downloads.append(("pdf", pdf_data))
    return downloads


def load_cached_reports(month: int, year: int, cache_dir: str | None) -> list[tuple[str, bytes]]:
    downloads: list[tuple[str, bytes]] = []
    excel_data = load_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.xlsx"))
    if excel_data:
        downloads.append(("excel", excel_data))
    pdf_data = load_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.pdf"))
    if pdf_data:
        downloads.append(("pdf", pdf_data))
    return downloads


def extract_pdftotext_pages(data: bytes) -> list[str]:
    pdftotext_bin = _which("pdftotext")
    if pdftotext_bin is None:
        return []
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as file_handle:
        file_handle.write(data)
        temp_path = file_handle.name
    try:
        completed = subprocess.run(
            [pdftotext_bin, "-layout", temp_path, "-"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    if completed.returncode != 0 or not completed.stdout.strip():
        return []
    return completed.stdout.split("\f")


def extract_layout_pages(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[list[str], str]:
    if pdfplumber is not None:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            pdfplumber_words = sum(len(page.extract_words() or []) for page in pdf.pages[:PDFPLUMBER_SAMPLE_PAGES])
            if pdfplumber_words >= PDFPLUMBER_MIN_WORDS:
                return [page.extract_text(x_tolerance=1, y_tolerance=1) or "" for page in pdf.pages], "pdfplumber"
            log(
                f"WARN pdfplumber extracted only {pdfplumber_words} words across the first "
                f"{PDFPLUMBER_SAMPLE_PAGES} pages; falling back to pdftotext"
            )
            pdftotext_pages = extract_pdftotext_pages(data)
            if pdftotext_pages:
                return pdftotext_pages, "pdftotext"
            return [page.extract_text(x_tolerance=1, y_tolerance=1) or "" for page in pdf.pages], "pdfplumber"
    pdftotext_pages = extract_pdftotext_pages(data)
    if pdftotext_pages:
        return pdftotext_pages, "pdftotext"
    return [], "pdfplumber"


def parse_sports_line(line: str, label: str) -> tuple[float, float] | None:
    if label not in line:
        return None
    suffix = line.split(label, 1)[1]
    match = _SPORTS_LINE_RE.search(suffix)
    if not match:
        return None
    win_amount = parse_dollar(match.group(1))
    win_pct = parse_dollar(match.group(3))
    if win_amount is None or win_pct is None or win_pct == 0:
        return None
    revenue = win_amount * 1000
    handle = revenue / (win_pct / 100)
    return handle, revenue


def parse_label_from_area_pages(
    page_texts: list[str],
    area_title: str,
    label: str,
) -> tuple[float, float] | None:
    for page_text in page_texts:
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        if area_title.lower() not in " ".join(lines[:6]).lower():
            continue
        for line in lines:
            parsed = parse_sports_line(line, label)
            if parsed is not None:
                return parsed
    return None


def parse_label_from_page_hint(
    page_texts: list[str],
    page_number: int,
    label: str,
    window: int = 1,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float] | None:
    start = max(1, page_number - window)
    stop = min(len(page_texts), page_number + window)
    candidates: list[tuple[int, float, float]] = []
    for current_page in range(start, stop + 1):
        page_text = page_texts[current_page - 1]
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        for line in lines:
            parsed = parse_sports_line(line, label)
            if parsed is not None:
                handle, revenue = parsed
                candidates.append((current_page, handle, revenue))
    if not candidates:
        return None
    best_page, best_handle, best_revenue = max(
        candidates,
        key=lambda item: (-abs(item[0] - page_number), abs(item[2])),
    )
    log(
        f"INFO selected {label} from hinted page window near page {page_number}: "
        f"page={best_page} revenue={best_revenue:,.0f}"
    )
    return best_handle, best_revenue


def find_text_anchor_page(page_texts: list[str], area_name: str) -> int | None:
    anchor = area_name.lower()
    for index, page_text in enumerate(page_texts, start=1):
        if anchor in page_text.lower():
            return index
    return None


def parse_label_from_anchor_or_hint(
    page_texts: list[str],
    area_name: str,
    label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float] | None:
    anchor_page = find_text_anchor_page(page_texts, area_name)
    if anchor_page is not None:
        parsed = parse_label_from_page_hint(page_texts, anchor_page, label, log=log)
        if parsed is not None:
            log(f"INFO [Nevada] {area_name} found via text anchor at page {anchor_page}")
            return parsed
    hint_page = NGCB_PDFTOTEXT_PAGE_HINTS[area_name]
    parsed = parse_label_from_page_hint(page_texts, hint_page, label, log=log)
    if parsed is not None:
        log(
            f"INFO [Nevada] {area_name} found via page hint (page {hint_page}) "
            "— verify if format changes"
        )
    return parsed


def reconcile_county_details(
    details: list[tuple[str, float, float]],
    statewide_revenue: float,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> list[tuple[str, float, float]]:
    reconciled: list[tuple[str, float, float]] = []
    for name, handle, revenue in details:
        if name in {"Clark County", "Washoe County"} and revenue > statewide_revenue + 1.0:
            log(
                f"WARN [Nevada] {name} revenue {revenue:,.0f} exceeds statewide revenue "
                f"{statewide_revenue:,.0f}; blanking county revenue"
            )
            continue
        reconciled.append((name, handle, revenue))
    return reconciled


def _build_nv_details(
    total_handle: float,
    total_revenue: float,
    clark: tuple[float, float] | None,
    washoe: tuple[float, float] | None,
    *,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> list[tuple[str, float, float]]:
    details: list[tuple[str, float, float]] = [("Statewide", total_handle, total_revenue)]
    if clark is not None:
        details.append(("Clark County", clark[0], clark[1]))
    if washoe is not None:
        details.append(("Washoe County", washoe[0], washoe[1]))
    details = reconcile_county_details(details, total_revenue, log=log)
    region_map = {
        region_name: (region_handle, region_revenue)
        for region_name, region_handle, region_revenue in details
    }
    clark = region_map.get("Clark County")
    washoe = region_map.get("Washoe County")
    if clark is not None and washoe is not None:
        details.append((
            "Other Areas",
            total_handle - clark[0] - washoe[0],
            total_revenue - clark[1] - washoe[1],
        ))
    return details


def parse_gri_pdftotext_fallback(
    page_texts: list[str],
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    total = parse_label_from_anchor_or_hint(page_texts, "Statewide", "Sports Pool (2)", log=log)
    mobile = parse_label_from_anchor_or_hint(page_texts, "Statewide", "Sports - Mobile", log=log)
    clark = parse_label_from_anchor_or_hint(page_texts, "Clark County", "Sports Pool (2)", log=log)
    washoe = parse_label_from_anchor_or_hint(page_texts, "Washoe County", "Sports Pool (2)", log=log)
    if total is None or mobile is None:
        return None
    total_handle, total_revenue = total
    mobile_handle, mobile_revenue = mobile
    retail_handle = total_handle - mobile_handle
    retail_revenue = total_revenue - mobile_revenue
    details = _build_nv_details(total_handle, total_revenue, clark, washoe, log=log)
    return total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details


def parse_area_sports_from_pages(page_texts: list[str], area_title: str) -> tuple[float, float] | None:
    return parse_label_from_area_pages(page_texts, area_title, "Sports Pool (2)")


def parse_gri_pdf(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    try:
        page_texts, extraction_source = extract_layout_pages(data, log=log)
    except Exception as exc:
        log(f"WARN could not parse Nevada PDF: {exc}")
        return None
    if not page_texts:
        return None
    total = parse_area_sports_from_pages(page_texts, AREA_LABELS["Statewide"])
    mobile = parse_label_from_area_pages(page_texts, AREA_LABELS["Statewide"], "Sports - Mobile")
    if total is None:
        if extraction_source != "pdftotext":
            return None
        parsed = parse_gri_pdftotext_fallback(page_texts, log=log)
        if parsed is None:
            return None
        log("INFO parsed Nevada PDF via pdftotext page-hint fallback")
        return parsed
    total_handle, total_revenue = total
    clark = parse_area_sports_from_pages(page_texts, AREA_LABELS["Clark County"])
    washoe = parse_area_sports_from_pages(page_texts, AREA_LABELS["Washoe County"])
    details = _build_nv_details(total_handle, total_revenue, clark, washoe, log=log)
    if mobile is None:
        return total_handle, total_revenue, total_handle, total_revenue, None, None, details
    mobile_handle, mobile_revenue = mobile
    retail_handle = total_handle - mobile_handle
    retail_revenue = total_revenue - mobile_revenue
    return total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details


def extract_numeric_cells(values: list[object]) -> list[float]:
    numbers: list[float] = []
    for value in values:
        parsed = parse_dollar(value)
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def parse_sports_row_values(values: list[object], label: str) -> tuple[float, float] | None:
    joined = " ".join(str(value or "") for value in values)
    if label.lower() not in joined.lower():
        return None
    numbers = extract_numeric_cells(values)
    if len(numbers) < 5:
        return None
    win_amount = numbers[2]
    win_pct = numbers[4]
    if win_pct == 0:
        return None
    revenue = win_amount * 1000
    handle = revenue / (win_pct / 100)
    return handle, revenue


def parse_gri_workbook(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    if openpyxl is None:
        log("WARN openpyxl not installed; cannot parse Excel report")
        return None
    try:
        data = ensure_xlsx(data, log=log)
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open Nevada workbook: {exc}")
        return None
    try:
        total = None
        mobile = None
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                values = list(row)
                total = total or parse_sports_row_values(values, "Sports Pool (2)")
                mobile = mobile or parse_sports_row_values(values, "Sports - Mobile")
                if total and mobile:
                    break
            if total and mobile:
                break
        if total is None:
            return None
        total_handle, total_revenue = total
        if mobile is None:
            return total_handle, total_revenue, total_handle, total_revenue, None, None, []
        mobile_handle, mobile_revenue = mobile
        retail_handle = total_handle - mobile_handle
        retail_revenue = total_revenue - mobile_revenue
        return total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, []
    finally:
        workbook.close()


def parse_report_bytes(
    kind: str,
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    if kind == "excel":
        parsed = parse_gri_workbook(data, log=log)
        if parsed is not None:
            return parsed
        log("WARN Excel parse failed; report will be skipped unless a PDF fallback exists")
        return None
    return parse_gri_pdf(data, log=log)


def parse_local_file(
    local_dir: str,
    month: int,
    year: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    full_month = date(year, month, 1).strftime("%B").lower()
    short_month = date(year, month, 1).strftime("%b").lower()
    for entry in sorted(os.listdir(local_dir)):
        if not entry.lower().endswith((".pdf", ".xlsx", ".xls")):
            continue
        lower = entry.lower()
        if str(year) in lower and (full_month in lower or short_month in lower):
            with open(os.path.join(local_dir, entry), "rb") as file_handle:
                kind = "excel" if lower.endswith((".xlsx", ".xls")) else "pdf"
                return parse_report_bytes(kind, file_handle.read(), log=log)
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
    """Fetch and parse Nevada monthly sports wagering results."""
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    discovered_urls: dict[tuple[int, int], dict[str, str]] | None = None
    results: list[StateMonthResult] = []
    for mdate in target_dates:
        month = mdate.month
        year = mdate.year
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(year, month)
        try:
            logger(f"INFO processing {label}")
            parsed = parse_local_file(local_dir, month, year, log=logger) if local_dir else None
            chosen_url: str | None = None
            if parsed:
                logger(f"INFO parsed local report for {label}")
                chosen_url = GRI_PAGE_URL
            if parsed is None:
                cached_reports = load_cached_reports(month, year, cache_dir)
                default_candidate = build_pdf_url_candidates(month, year)[0]
                if cached_reports:
                    logger(f"INFO using cached report file(s) for {label}")
                elif not offline:
                    if discovered_urls is None:
                        discovered_urls = discover_report_urls(log=logger)
                        if report is not None:
                            report.add_index(GRI_PAGE_URL, anchor_count=len(discovered_urls))
                            for (m, y), kinds in discovered_urls.items():
                                lab = datetime(y, m, 1).strftime("%b %Y")
                                for kind, url in kinds.items():
                                    report.add_anchor(f"{lab}:{kind}", url)
                    cached_reports = download_report(month, year, discovered_urls, cache_dir, log=logger)
                    if report is not None:
                        report.add_probe(
                            f"{label}:report",
                            f"discovered_urls[{label}]",
                            status="ok" if cached_reports else "fail",
                            reason="downloaded" if cached_reports else "no-candidates",
                        )
                else:
                    logger(f"INFO offline cache miss for nevada_{year}_{month:02d}")
                disc_for_month = (discovered_urls or {}).get((month, year), {})
                for kind, data in cached_reports:
                    parsed = parse_report_bytes(kind, data, log=logger)
                    if parsed is not None:
                        chosen_url = disc_for_month.get(kind) or (
                            GRI_PAGE_URL if kind == "excel" else default_candidate
                        )
                        break
            if parsed is None:
                log_skip(logger, label, "no parseable GRI report")
                continue
            handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details = parsed
            region_map = {
                region_name: (region_handle, region_revenue)
                for region_name, region_handle, region_revenue in details
            }
            extra: dict[str, float | None] = {}
            for region_name in REGION_ORDER:
                handle_key, revenue_key = REGION_EXTRA_KEYS[region_name]
                region_handle, region_revenue = region_map.get(region_name, (None, None))
                extra[handle_key] = region_handle
                extra[revenue_key] = region_revenue
            item = finalize_state_month(
                state=STATE_NAME,
                iso_month=iso_month,
                handle=handle,
                revenue=revenue,
                retail_handle=retail_handle,
                retail_revenue=retail_revenue,
                mobile_handle=mobile_handle,
                mobile_revenue=mobile_revenue,
                source_url=chosen_url,
                extra=extra,
                log=logger,
                label=label,
            )
            if item is None:
                continue
            results.append(item)
            logger(
                f"INFO {label}: handle={item.handle:,.0f} revenue={item.revenue:,.0f} "
                f"retail_handle={item.retail_handle if item.retail_handle is not None else 'n/a'} "
                f"mobile_handle={item.mobile_handle if item.mobile_handle is not None else 'n/a'} "
                f"hold={(item.hold or 0.0):.2f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
