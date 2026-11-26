"""Nevada sports wagering regulatory fetcher and parser (NGCB GRI)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import re
from typing import Callable, Sequence
from urllib.parse import urljoin

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_age_seconds, cache_path, find_local_month_file, load_cache, recent_cache_ttl, try_save_cache
from ..discover import DiscoveryReport, extract_month_label, parse_anchors
from ..http import fetch_bytes
from ..model import (
    MONTH_ABBR,
    MONTH_NAMES,
    StateMonthResult,
    build_region_extra,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_month_result,
    log_skip,
    parse_month_spec,
    resolve_fetch_months,
)
from ..parse import (
    PDFPLUMBER_LOCK,
    ensure_xlsx,
    extract_pdf_text,
    is_pdf_bytes,
    is_xls_bytes,
    open_xlsx_workbook,
    parse_dollar,
)

STATE_NAME = "Nevada"
LAUNCH_DATE: date | None = None
REGION_ORDER = ["Clark County", "Washoe County", "Other Areas"]
REGION_EXTRA_KEYS = {
    "Clark County": ("Clark Handle", "Clark Revenue"),
    "Washoe County": ("Washoe Handle", "Washoe Revenue"),
    "Other Areas": ("Other Handle", "Other Revenue"),
}
EXTRA_HEADERS: list[str] = [
    key for region in REGION_ORDER for key in REGION_EXTRA_KEYS[region]
]
HAS_DETAILS = False

BASE = "https://gaming.nv.gov"
USER_AGENT = "Mozilla/5.0"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
GRI_PAGE_URL = f"{BASE}/about-us/gaming-revenue-information-gri/"
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

_DIGIT_ALPHA_SPLIT_RE = re.compile(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", re.IGNORECASE)
_SPORTS_LINE_RE = re.compile(r"(?:\d+\s+){0,3}([+\-\u2212()\d,.]+)\s+([+\-\u2212()\d,.]+)\s+([+\-\u2212()\d,.]+)")

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
    label = extract_month_label(text)
    if not label:
        return None
    d = parse_month_spec(label)
    return d.month, d.year


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
    urls: dict[tuple[int, int], dict[str, str]] = {}
    for link in parse_anchors(html, url=GRI_PAGE_URL, log=log):
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
        label = f"{MONTH_ABBR[month]} {year}"
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
    month_name = MONTH_NAMES[month].lower()
    month_short = MONTH_ABBR[month]
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


def _fetch_nv_safe(url: str, log: Callable[[str], None]) -> bytes | None:
    try:
        return fetch_nv_bytes(url)
    except Exception as exc:
        log(f"WARN could not download {url}: {exc}")
        return None


def download_pdf(month: int, year: int, log: Callable[[str], None] = _DEFAULT_LOG) -> bytes | None:
    for url in build_pdf_url_candidates(month, year):
        data = _fetch_nv_safe(url, log)
        if is_pdf_bytes(data):
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
        data = _fetch_nv_safe(url, log)
        if data:
            valid = is_xls_bytes(data) if kind == "excel" else is_pdf_bytes(data)
            if not valid:
                log(f"WARN discarding unexpected {kind} payload from {url}")
                continue
            log(f"INFO downloaded {url}")
            extension = "xlsx" if kind == "excel" else "pdf"
            try_save_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.{extension}"), data, log=log)
            downloads.append((kind, data))
    if not any(kind == "pdf" for kind, _ in downloads):
        pdf_data = download_pdf(month, year, log=log)
        if pdf_data:
            try_save_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.pdf"), pdf_data, log=log)
            downloads.append(("pdf", pdf_data))
    return downloads


def load_cached_reports(month: int, year: int, cache_dir: str | None) -> list[tuple[str, bytes]]:
    downloads: list[tuple[str, bytes]] = []
    excel_data = load_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.xlsx"))
    if is_xls_bytes(excel_data):
        assert excel_data is not None
        downloads.append(("excel", excel_data))
    pdf_data = load_cache(cache_path(cache_dir, f"nevada_{year}_{month:02d}.pdf"))
    if is_pdf_bytes(pdf_data):
        assert pdf_data is not None
        downloads.append(("pdf", pdf_data))
    return downloads


def extract_pdftotext_pages(data: bytes) -> list[str]:
    text = extract_pdf_text(data, layout=True, use_pdfplumber=False)
    if not text or not text.strip():
        return []
    return text.split("\f")


def extract_layout_pages(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> tuple[list[str], str]:
    fallback_pages: list[str] = []
    if pdfplumber is not None:
        with PDFPLUMBER_LOCK, pdfplumber.open(io.BytesIO(data)) as pdf:
            pdfplumber_words = sum(len(page.extract_words() or []) for page in pdf.pages[:PDFPLUMBER_SAMPLE_PAGES])
            fallback_pages = [page.extract_text(x_tolerance=1, y_tolerance=1) or "" for page in pdf.pages]
            if pdfplumber_words >= PDFPLUMBER_MIN_WORDS:
                return fallback_pages, "pdfplumber"
        log(
            f"WARN pdfplumber extracted only {pdfplumber_words} words across the first "
            f"{PDFPLUMBER_SAMPLE_PAGES} pages; falling back to pdftotext"
        )
    pdftotext_pages = extract_pdftotext_pages(data)
    if pdftotext_pages:
        return pdftotext_pages, "pdftotext"
    return fallback_pages, "pdfplumber"


def _metrics_from_win(win_amount: float | None, win_pct: float | None) -> tuple[float, float] | None:
    if win_amount is None or win_pct is None or abs(win_pct) < 1e-9:
        return None
    if win_amount != 0 and (win_amount < 0) != (win_pct < 0):
        return None
    revenue = win_amount * 1000
    handle = revenue / (win_pct / 100)
    return handle, revenue


def parse_sports_line(line: str, label: str) -> tuple[float, float] | None:
    if label not in line:
        return None
    suffix = line.split(label, 1)[1]
    match = _SPORTS_LINE_RE.search(suffix)
    if not match:
        return None
    return _metrics_from_win(parse_dollar(match.group(1)), parse_dollar(match.group(3)))


def _prepare_area_pages(page_texts: list[str]) -> list[tuple[str, list[str]]]:
    prepared: list[tuple[str, list[str]]] = []
    for page_text in page_texts:
        lines = [line.strip() for line in page_text.splitlines() if line.strip()]
        prepared.append((" ".join(lines[:6]).lower(), lines))
    return prepared


def _scan_area_pages(
    prepared_pages: list[tuple[str, list[str]]],
    area_title: str,
    labels: tuple[str, ...],
) -> dict[str, tuple[float, float]]:
    area_lower = area_title.lower()
    found: dict[str, tuple[float, float]] = {}
    for header_low, lines in prepared_pages:
        if area_lower not in header_low:
            continue
        for line in lines:
            for target_label in labels:
                if target_label in found:
                    continue
                parsed = parse_sports_line(line, target_label)
                if parsed is not None:
                    found[target_label] = parsed
                    if len(found) == len(labels):
                        return found
    return found


def parse_label_from_page_hint(
    page_texts: list[str],
    page_number: int,
    label: str,
    window: int = 1,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    _lines_cache: dict[int, list[str]] | None = None,
) -> tuple[float, float] | None:
    start = max(1, page_number - window)
    stop = min(len(page_texts), page_number + window)
    candidates: list[tuple[int, float, float]] = []
    for current_page in range(start, stop + 1):
        if _lines_cache is not None and current_page in _lines_cache:
            lines = _lines_cache[current_page]
        else:
            page_text = page_texts[current_page - 1]
            lines = [line.strip() for line in page_text.splitlines() if line.strip()]
            if _lines_cache is not None:
                _lines_cache[current_page] = lines
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


def find_text_anchor_page(
    page_texts: list[str],
    area_name: str,
    *,
    _pages_lower: list[str] | None = None,
) -> int | None:
    anchor = area_name.lower()
    haystacks = _pages_lower if _pages_lower is not None else [pt.lower() for pt in page_texts]
    for index, page_lower in enumerate(haystacks, start=1):
        if anchor in page_lower:
            return index
    return None


def parse_label_from_anchor_or_hint(
    page_texts: list[str],
    area_name: str,
    label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    _pages_lower: list[str] | None = None,
    _lines_cache: dict[int, list[str]] | None = None,
) -> tuple[float, float] | None:
    anchor_page = find_text_anchor_page(page_texts, area_name, _pages_lower=_pages_lower)
    if anchor_page is not None:
        parsed = parse_label_from_page_hint(
            page_texts, anchor_page, label, log=log, _lines_cache=_lines_cache,
        )
        if parsed is not None:
            log(f"INFO [Nevada] {area_name} found via text anchor at page {anchor_page}")
            return parsed
    hint_page = NGCB_PDFTOTEXT_PAGE_HINTS[area_name]
    parsed = parse_label_from_page_hint(
        page_texts, hint_page, label, log=log, _lines_cache=_lines_cache,
    )
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
        if name in {"Clark County", "Washoe County"} and statewide_revenue >= 0 and revenue > statewide_revenue + 1.0:
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
    kept_clark = next(((h, r) for n, h, r in details if n == "Clark County"), None)
    kept_washoe = next(((h, r) for n, h, r in details if n == "Washoe County"), None)
    if kept_clark is not None and kept_washoe is not None:
        details.append((
            "Other Areas",
            total_handle - kept_clark[0] - kept_washoe[0],
            total_revenue - kept_clark[1] - kept_washoe[1],
        ))
    return details


def parse_gri_pdftotext_fallback(
    page_texts: list[str],
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    pages_lower = [pt.lower() for pt in page_texts]
    lines_cache: dict[int, list[str]] = {}
    total = parse_label_from_anchor_or_hint(
        page_texts, "Statewide", "Sports Pool (2)", log=log, _pages_lower=pages_lower, _lines_cache=lines_cache,
    )
    mobile = parse_label_from_anchor_or_hint(
        page_texts, "Statewide", "Sports - Mobile", log=log, _pages_lower=pages_lower, _lines_cache=lines_cache,
    )
    if total is None or mobile is None:
        return None
    clark = parse_label_from_anchor_or_hint(
        page_texts, "Clark County", "Sports Pool (2)", log=log, _pages_lower=pages_lower, _lines_cache=lines_cache,
    )
    washoe = parse_label_from_anchor_or_hint(
        page_texts, "Washoe County", "Sports Pool (2)", log=log, _pages_lower=pages_lower, _lines_cache=lines_cache,
    )
    total_handle, total_revenue = total
    mobile_handle, mobile_revenue = mobile
    retail_handle = total_handle - mobile_handle
    retail_revenue = total_revenue - mobile_revenue
    details = _build_nv_details(total_handle, total_revenue, clark, washoe, log=log)
    return total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details


def _with_mobile_splits(
    total_handle: float,
    total_revenue: float,
    mobile: tuple[float, float] | None,
    details: list[tuple[str, float, float]],
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]]:
    if mobile is None:
        return total_handle, total_revenue, None, None, None, None, details
    mobile_handle, mobile_revenue = mobile
    return (
        total_handle,
        total_revenue,
        total_handle - mobile_handle,
        total_revenue - mobile_revenue,
        mobile_handle,
        mobile_revenue,
        details,
    )


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
    prepared_pages = _prepare_area_pages(page_texts)
    statewide_hits = _scan_area_pages(
        prepared_pages, AREA_LABELS["Statewide"], ("Sports Pool (2)", "Sports - Mobile"),
    )
    total = statewide_hits.get("Sports Pool (2)")
    mobile = statewide_hits.get("Sports - Mobile")
    if total is None:
        if extraction_source != "pdftotext":
            return None
        parsed = parse_gri_pdftotext_fallback(page_texts, log=log)
        if parsed is None:
            return None
        log("INFO parsed Nevada PDF via pdftotext page-hint fallback")
        return parsed
    total_handle, total_revenue = total
    clark = _scan_area_pages(prepared_pages, AREA_LABELS["Clark County"], ("Sports Pool (2)",)).get("Sports Pool (2)")
    washoe = _scan_area_pages(prepared_pages, AREA_LABELS["Washoe County"], ("Sports Pool (2)",)).get("Sports Pool (2)")
    details = _build_nv_details(total_handle, total_revenue, clark, washoe, log=log)
    return _with_mobile_splits(total_handle, total_revenue, mobile, details)


_NV_EXPLICIT_PLACEHOLDERS = frozenset({"+", "-", "\u2014", "\u2013", "n/a", "na", "none", "null"})


def extract_numeric_cells(values: Sequence[object]) -> list[float]:
    numbers: list[float] = []
    for value in values:
        parsed = parse_dollar(value)
        if parsed is not None:
            numbers.append(parsed)
    return numbers


def _extract_numeric_or_placeholder_slots(values: Sequence[object]) -> list[float | None]:
    slots: list[float | None] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text:
            continue
        parsed = parse_dollar(value)
        if parsed is not None:
            slots.append(parsed)
        elif text.lower() in _NV_EXPLICIT_PLACEHOLDERS:
            slots.append(None)
    return slots


def parse_sports_row_values(
    values: Sequence[object],
    label: str,
    *,
    _joined_low: str | None = None,
) -> tuple[float, float] | None:
    joined_low = (
        _joined_low
        if _joined_low is not None
        else " ".join(str(value or "") for value in values).lower()
    )
    if label.lower() not in joined_low:
        return None
    slots = _extract_numeric_or_placeholder_slots(values)
    if len(slots) < 5:
        return None
    return _metrics_from_win(slots[2], slots[4])


def parse_gri_workbook(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float, float, float | None, float | None, float | None, float | None, list[tuple[str, float, float]]] | None:
    if openpyxl is None:
        log("WARN openpyxl not installed; cannot parse Excel report")
        return None
    converted = ensure_xlsx(data, log=log)
    with open_xlsx_workbook(
        converted, openpyxl_mod=openpyxl, log=log, error_prefix="could not open Nevada workbook",
    ) as workbook:
        if workbook is None:
            return None
        total = None
        mobile = None
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                if not any(cell is not None for cell in row):
                    continue
                joined_low = " ".join(str(cell) for cell in row if cell is not None).lower()
                if total is None:
                    total = parse_sports_row_values(row, "Sports Pool (2)", _joined_low=joined_low)
                if mobile is None:
                    mobile = parse_sports_row_values(row, "Sports - Mobile", _joined_low=joined_low)
                if total and mobile:
                    break
            if total and mobile:
                break
        if total is None:
            return None
        return _with_mobile_splits(total[0], total[1], mobile, [])


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
    matched = find_local_month_file(local_dir, year, month, (".pdf", ".xlsx", ".xls"))
    if matched is None:
        return None
    entry, raw = matched
    kind = "excel" if entry.lower().endswith((".xlsx", ".xls")) else "pdf"
    return parse_report_bytes(kind, raw, log=log)


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
        label = f"{MONTH_ABBR[month]} {year}"
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
                ttl_seconds = recent_cache_ttl(year, month)
                fresh_reports: list[tuple[str, bytes]] = []
                stale_reports: list[tuple[str, bytes]] = []
                for kind, data in cached_reports:
                    cpath = cache_path(cache_dir, f"nevada_{year}_{month:02d}.{'xlsx' if kind == 'excel' else 'pdf'}")
                    age = cache_age_seconds(cpath) if not offline and ttl_seconds is not None else 0.0
                    if not offline and ttl_seconds is not None and (age is None or age > ttl_seconds):
                        stale_reports.append((kind, data))
                    else:
                        fresh_reports.append((kind, data))
                if fresh_reports:
                    logger(f"INFO using cached report file(s) for {label}")
                    for kind, data in fresh_reports:
                        parsed = parse_report_bytes(kind, data, log=logger)
                        if parsed is not None:
                            chosen_url = None
                            break
                if parsed is None and not offline:
                    if discovered_urls is None:
                        discovered_urls = discover_report_urls(log=logger)
                        if report is not None:
                            report.add_index(GRI_PAGE_URL, anchor_count=len(discovered_urls))
                            for (m, y), kinds in discovered_urls.items():
                                lab = f"{MONTH_ABBR[m]} {y}"
                                for kind, url in kinds.items():
                                    report.add_anchor(f"{lab}:{kind}", url)
                    downloaded_reports = download_report(month, year, discovered_urls, cache_dir, log=logger)
                    if report is not None:
                        report.add_probe(
                            f"{label}:report",
                            f"discovered_urls[{label}]",
                            status="ok" if downloaded_reports else "fail",
                            reason="downloaded" if downloaded_reports else "no-candidates",
                        )
                    disc_for_month = (discovered_urls or {}).get((month, year), {})
                    for kind, data in downloaded_reports:
                        parsed = parse_report_bytes(kind, data, log=logger)
                        if parsed is not None:
                            chosen_url = disc_for_month.get(kind)
                            break
                    if parsed is None:
                        for kind, data in stale_reports:
                            parsed = parse_report_bytes(kind, data, log=logger)
                            if parsed is not None:
                                chosen_url = None
                                break
                elif parsed is None and offline:
                    logger(f"INFO offline cache miss for nevada_{year}_{month:02d}")
            if parsed is None:
                log_skip(logger, label, "no parseable GRI report")
                continue
            handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details = parsed
            extra = build_region_extra(details, REGION_ORDER, REGION_EXTRA_KEYS)
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
            log_month_result(logger, label, item, hold_decimals=2)
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
