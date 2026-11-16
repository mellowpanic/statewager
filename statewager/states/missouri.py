"""Missouri sports wagering regulatory fetcher and parser (MGC)."""
from __future__ import annotations

from collections import OrderedDict
from datetime import date, datetime
from html import unescape
import io
import os
import re
from typing import Callable
from urllib.parse import urljoin

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from ..cache import cache_path, load_cached_or_fetch
from ..discover import MONTH_NAMES, TEXT_MONTH_RE, DiscoveryReport, crawl_anchors
from ..http import fetch_with_wayback_fallback
from ..model import (
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    fiscal_year_july,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
)
from ..parse import parse_dollar

STATE_NAME = "Missouri"
LAUNCH_DATE: date | None = date(2025, 12, 1)
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

MGC_BASE = "https://www.mgc.dps.mo.gov"
MGC_SW_REPORTS_PAGE = f"{MGC_BASE}/SportsWagering/sw_financials/rb_SWFin_main.html"
MGC_FINANCIALS_PAGE = f"{MGC_BASE}/Casino_Gaming/rb_financials/rb_Fin_main.html"
BASE_URL = (
    f"{MGC_BASE}/SportsWagering/sw_financials/"
    "FY{fy}_SWFinReport/{mm}_{mon}/SW%20Monthly%20Financials%20{mmyy}.pdf"
)

_DEFAULT_LOG = build_state_logger("MO")

PDF_NAME_RE = re.compile(
    r"SW(?:%20|[\s_])+Monthly(?:%20|[\s_])+Financials?(?:%20|[\s_])+(\d{4})(?:(?:%20|[\s_])*[a-z0-9_-]*)?\.pdf",
    re.IGNORECASE,
)
ROW_MONTH_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{2}\b", re.IGNORECASE)
_DOLLAR_TOKEN_RE = re.compile(r"[+\-\u2212]?\(?\$[\d,]+(?:\.\d+)?\)?")
_MOBILE_WORD_RE = re.compile(r"\bmobile\b")
_RETAIL_WORD_RE = re.compile(r"\bretail\b")
_WS_RE = re.compile(r"\s+")
_CHANNEL_SUFFIX_RE = re.compile(r"\s*-\s*(retail|mobile)\s*$", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def fiscal_year(year: int, month: int) -> int:
    return fiscal_year_july(year, month)[1] % 100


def build_pdf_url(year: int, month: int) -> str:
    return BASE_URL.format(
        fy=fiscal_year(year, month),
        mm=f"{month:02d}",
        mon=date(year, month, 1).strftime("%b"),
        mmyy=f"{month:02d}{year % 100:02d}",
    )


def discover_sw_reports_page_from_fallback(
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> str | None:
    """Try to locate the SW reports page by crawling the general casino financials index."""
    log("INFO falling back to MGC casino financials index to locate SW reports page...")
    links = crawl_anchors(MGC_FINANCIALS_PAGE, log=log)
    if not links:
        log("WARN could not fetch MGC casino financials index")
        return None
    for link in links:
        href = unescape(link.get("href", "").strip())
        meta = " ".join(unescape(link.get(key, "")) for key in ("title", "text")).lower()
        if "rb_swfin_main.html" in href.lower() or ("sports" in meta and "financial" in meta):
            return urljoin(MGC_FINANCIALS_PAGE, href)
    return None


def _fetch_reports_page(
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[str, list[dict[str, str]]] | None:
    """Crawl the SW reports page directly; fall back to discovery via casino index."""
    candidates: list[str] = [MGC_SW_REPORTS_PAGE]
    for page in candidates:
        links = crawl_anchors(page, log=log)
        if links:
            if report is not None:
                report.add_index(page, anchor_count=len(links))
            return page, links
    fallback = discover_sw_reports_page_from_fallback(log=log)
    if fallback and fallback not in candidates:
        links = crawl_anchors(fallback, log=log)
        if links:
            if report is not None:
                report.add_index(fallback, anchor_count=len(links))
            return fallback, links
    return None


def scrape_pdf_urls(
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[str, str]:
    log(f"INFO scraping MGC sports wagering reports page: {MGC_SW_REPORTS_PAGE}")
    fetched = _fetch_reports_page(report=report, log=log)
    if not fetched:
        log("WARN could not fetch MGC sports wagering reports page (primary or fallback)")
        return {}
    reports_page, links = fetched
    urls: dict[str, str] = {}
    for link in links:
        href = unescape(link.get("href", "").strip())
        if not href.lower().endswith(".pdf"):
            if report is not None and href:
                report.add_classification(
                    href, title=link.get("title", ""), text=link.get("text", ""), reason="not-pdf",
                )
            continue
        lower_href = href.lower()
        if "monthly" not in lower_href or "financial" not in lower_href:
            if report is not None:
                report.add_classification(
                    href, title=link.get("title", ""), text=link.get("text", ""), reason="not-monthly-financial",
                )
            continue
        match = PDF_NAME_RE.search(href)
        if match:
            mmyy = match.group(1)
            month_num = int(mmyy[:2])
            if not (1 <= month_num <= 12):
                continue
            year = 2000 + int(mmyy[2:])
        else:
            meta = " ".join(unescape(link.get(key, "")) for key in ("title", "text"))
            text_match = TEXT_MONTH_RE.search(meta)
            if not text_match:
                if report is not None:
                    report.add_classification(
                        href, title=link.get("title", ""), text=link.get("text", ""), reason="no-month-token",
                    )
                continue
            month_num = MONTH_NAMES[text_match.group(1).lower()]
            year = int(text_match.group(2))
        label = datetime(year, month_num, 1).strftime("%b %Y")
        full_url = urljoin(reports_page, href)
        if label not in urls:
            urls[label] = full_url
            if report is not None:
                report.add_anchor(f"{label}:summary", full_url)
    log(f"INFO scraped {len(urls)} monthly financial PDF links from MGC")
    return urls


def extract_amounts(line: str) -> tuple[float, float] | None:
    amounts = [parse_dollar(token) for token in _DOLLAR_TOKEN_RE.findall(line)]
    amounts = [amount for amount in amounts if amount is not None]
    if len(amounts) < 3:
        return None
    return amounts[0], amounts[2]


def parse_section_rows(lines: list[str], month_token: str) -> tuple[float, float] | None:
    handle = 0.0
    revenue = 0.0
    matched_rows = 0
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith(month_token):
            continue
        parsed = extract_amounts(stripped)
        if parsed is None:
            continue
        handle += parsed[0]
        revenue += parsed[1]
        matched_rows += 1
    if matched_rows == 0:
        return None
    return handle, revenue


def parse_state_totals_line(line: str) -> tuple[float, float] | None:
    if not line.strip().startswith("STATE TOTALS MTD:"):
        return None
    return extract_amounts(line)


def parse_report_state_totals(page_texts: list[str], report_name: str) -> tuple[float, float] | None:
    in_report = False
    for text in page_texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            upper = line.upper()
            if report_name in upper:
                in_report = True
                continue
            if not in_report:
                continue
            parsed = parse_state_totals_line(line)
            if parsed is not None:
                return parsed
    return None


def clean_operator_name(operator: str) -> str:
    cleaned = _WS_RE.sub(" ", operator).strip(" -")
    cleaned = _CHANNEL_SUFFIX_RE.sub("", cleaned)
    return cleaned.strip(" -")


def detail_operator_key(operator: str) -> str:
    return _NON_ALNUM_RE.sub(" ", clean_operator_name(operator).lower()).strip()


def _fiscal_operator_key(operator: str) -> str:
    cleaned = clean_operator_name(operator).replace("'", "").replace("\u2019", "")
    normalized = _NON_ALNUM_RE.sub(" ", cleaned.lower()).strip()
    return normalized.replace("fanatics sportsbook", "fanatics")


def parse_licensee_line(line: str, month_token: str) -> tuple[str, float, float] | None:
    if "TOTALS:" in line.upper() or "STATE TOTALS" in line.upper():
        return None
    if month_token not in line:
        return None
    prefix, _, _ = line.partition(month_token)
    operator = clean_operator_name(prefix)
    if not operator:
        return None
    parsed = extract_amounts(line)
    if parsed is None:
        return None
    return operator, parsed[0], parsed[1]


def parse_report_details(
    page_texts: list[str],
    year: int,
    month: int,
) -> list[tuple[str, float, float, float | None, float | None, float | None, float | None]]:
    month_token = date(year, month, 1).strftime("%b-%y")
    details: OrderedDict[str, dict[str, float | str | None]] = OrderedDict()
    section = None
    current_operator = None

    def add_detail(operator: str, handle: float, revenue: float, current_section: str) -> None:
        display_name = clean_operator_name(operator)
        if not display_name:
            return
        key = detail_operator_key(display_name)
        detail = details.setdefault(
            key,
            {
                "name": display_name,
                "retail_handle": 0.0,
                "retail_revenue": 0.0,
                "mobile_handle": 0.0,
                "mobile_revenue": 0.0,
            },
        )
        if current_section == "retail":
            detail["retail_handle"] = float(detail["retail_handle"] or 0.0) + handle
            detail["retail_revenue"] = float(detail["retail_revenue"] or 0.0) + revenue
        elif current_section == "mobile":
            detail["mobile_handle"] = float(detail["mobile_handle"] or 0.0) + handle
            detail["mobile_revenue"] = float(detail["mobile_revenue"] or 0.0) + revenue

    for text in page_texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            upper = line.upper()
            if "STATISTICAL REPORT SUMMARY" in upper or "DETAIL REPORT" in upper:
                section = None
                current_operator = None
                continue
            if "SPORTS WAGERING RETAIL STATISTICAL REPORT" in upper:
                section = "retail"
                current_operator = None
                continue
            if "SPORTS WAGERING MOBILE STATISTICAL REPORT" in upper:
                section = "mobile"
                current_operator = None
                continue
            if section is None:
                continue
            if "STATE TOTALS" in upper or "TOTALS:" in upper:
                continue
            month_match = ROW_MONTH_RE.search(line)
            if month_match and _CHANNEL_SUFFIX_RE.search(line[:month_match.start()]):
                current_operator = clean_operator_name(line[:month_match.start()])
            inline = parse_licensee_line(line, month_token)
            if inline is not None:
                current_operator = inline[0]
                add_detail(inline[0], inline[1], inline[2], section)
                continue
            if current_operator and line.startswith(month_token):
                parsed = extract_amounts(line)
                if parsed is not None:
                    add_detail(current_operator, parsed[0], parsed[1], section)
                continue
    output: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
    for detail in details.values():
        retail_handle = float(detail["retail_handle"] or 0.0)
        retail_revenue = float(detail["retail_revenue"] or 0.0)
        mobile_handle = float(detail["mobile_handle"] or 0.0)
        mobile_revenue = float(detail["mobile_revenue"] or 0.0)
        output.append(
            (
                str(detail["name"]),
                retail_handle + mobile_handle,
                retail_revenue + mobile_revenue,
                retail_handle,
                retail_revenue,
                mobile_handle,
                mobile_revenue,
            )
        )
    return output


def _extract_all_dollar_amounts(line: str) -> list[float]:
    return [
        amount
        for token in _DOLLAR_TOKEN_RE.findall(line)
        if (amount := parse_dollar(token)) is not None
    ]


def _parse_page_texts_fiscal_full(
    page_texts: list[str],
    year: int,
    month: int,
) -> tuple[float | None, float | None, dict[str, float], dict[str, float]]:
    """Extract ``(state_gross_rev, state_tax, {_fiscal_operator_key: tax}, {_fiscal_operator_key: gross_rev})``."""
    month_token = date(year, month, 1).strftime("%b-%y")
    stat_taxes: dict[str, float] = {}
    detail_gross: dict[str, float] = {}
    op_taxes: dict[str, float] = {}
    op_gross: dict[str, float] = {}

    mode: str | None = None  # "stat_retail", "stat_mobile", "detail_retail", "detail_mobile"
    current_operator: str | None = None
    in_summary = False

    for text in page_texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            upper = line.upper()
            if "STATISTICAL REPORT SUMMARY" in upper:
                mode = None
                current_operator = None
                in_summary = True
                continue
            if "SPORTS WAGERING RETAIL STATISTICAL REPORT" in upper:
                if mode != "stat_retail":
                    current_operator = None
                mode = "stat_retail"
                in_summary = False
                continue
            if "SPORTS WAGERING MOBILE STATISTICAL REPORT" in upper:
                if mode != "stat_mobile":
                    current_operator = None
                mode = "stat_mobile"
                in_summary = False
                continue
            if "RETAIL DETAIL REPORT" in upper:
                if mode != "detail_retail":
                    current_operator = None
                mode = "detail_retail"
                in_summary = False
                continue
            if "MOBILE DETAIL REPORT" in upper:
                if mode != "detail_mobile":
                    current_operator = None
                mode = "detail_mobile"
                in_summary = False
                continue
            if "DETAIL REPORT" in upper:
                mode = None
                current_operator = None
                in_summary = False
                continue
            if in_summary or mode is None:
                continue

            if line.startswith("STATE TOTALS MTD:") or line.startswith("STATE TOTALS:"):
                current_operator = None
                amounts = _extract_all_dollar_amounts(line)
                if mode in ("stat_retail", "stat_mobile") and len(amounts) >= 4:
                    stat_taxes.setdefault(mode, amounts[3])
                elif mode in ("detail_retail", "detail_mobile") and len(amounts) >= 3:
                    detail_gross.setdefault(mode, amounts[2])
                continue

            if mode in ("detail_retail", "detail_mobile"):
                if current_operator and line.startswith("TOTALS:"):
                    amounts = _extract_all_dollar_amounts(line)
                    if len(amounts) >= 3:
                        k = _fiscal_operator_key(current_operator)
                        op_gross[k] = op_gross.get(k, 0.0) + amounts[2]
                    current_operator = None
                elif "TOTALS" not in upper and month_token in line:
                    prefix, _, _ = line.partition(month_token)
                    op_name = clean_operator_name(prefix)
                    if op_name:
                        current_operator = op_name
                continue

            if "STATE TOTALS" in upper or "TOTALS:" in upper:
                continue

            month_match = ROW_MONTH_RE.search(line)
            if month_match and _CHANNEL_SUFFIX_RE.search(line[:month_match.start()]):
                current_operator = clean_operator_name(line[:month_match.start()])
            if month_token in line:
                prefix, _, _ = line.partition(month_token)
                op_name = clean_operator_name(prefix) or current_operator
                if op_name:
                    current_operator = op_name
                    amounts = _extract_all_dollar_amounts(line)
                    if len(amounts) >= 4:
                        k = _fiscal_operator_key(op_name)
                        op_taxes[k] = op_taxes.get(k, 0.0) + amounts[3]

    state_gross_rev = sum(detail_gross.values()) if detail_gross else None
    state_tax = sum(stat_taxes.values()) if stat_taxes else None
    return state_gross_rev, state_tax, op_taxes, op_gross


def parse_page_texts_fiscal(
    page_texts: list[str],
    year: int,
    month: int,
) -> tuple[float | None, float | None, dict[str, float]]:
    """Extract ``(state_gross_revenue, state_tax, {operator_key: tax})`` from MGC page texts."""
    state_gross_rev, state_tax, op_taxes, _op_gross = _parse_page_texts_fiscal_full(page_texts, year, month)
    return state_gross_rev, state_tax, op_taxes


def _parse_pdf_with_fiscal(
    data: bytes,
    year: int,
    month: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    tuple[
        float,
        float,
        float | None,
        float | None,
        float | None,
        float | None,
        list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
    ],
    float | None,
    float | None,
    dict[str, float],
    dict[str, float],
] | None:
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is required to parse Missouri PDFs")
    month_token = date(year, month, 1).strftime("%b-%y")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_texts = [page.extract_text() or "" for page in pdf.pages]
            retail = parse_report_state_totals(page_texts, "SPORTS WAGERING RETAIL STATISTICAL REPORT")
            mobile = parse_report_state_totals(page_texts, "SPORTS WAGERING MOBILE STATISTICAL REPORT")
            if retail is not None and mobile is not None:
                details = parse_report_details(page_texts, year, month)
                state_gross_rev, state_tax, op_taxes, op_gross = _parse_page_texts_fiscal_full(
                    page_texts, year, month,
                )
                core = (
                    retail[0] + mobile[0],
                    retail[1] + mobile[1],
                    retail[0],
                    retail[1],
                    mobile[0],
                    mobile[1],
                    details,
                )
                return core, state_gross_rev, state_tax, op_taxes, op_gross
            text = "\n".join(page_texts)
    except Exception as exc:
        log(f"WARN could not parse Missouri PDF: {exc}")
        return None
    retail_lines: list[str] = []
    mobile_lines: list[str] = []
    section = None
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if _MOBILE_WORD_RE.search(lower) and "retail" not in lower:
            section = "mobile"
            continue
        if _RETAIL_WORD_RE.search(lower) and "mobile" not in lower:
            section = "retail"
            continue
        if section == "mobile":
            mobile_lines.append(stripped)
        elif section == "retail":
            retail_lines.append(stripped)
    retail = parse_section_rows(retail_lines, month_token)
    mobile = parse_section_rows(mobile_lines, month_token)
    if retail is None or mobile is None:
        return None
    total = (retail[0] + mobile[0], retail[1] + mobile[1])
    core = (
        total[0],
        total[1],
        retail[0],
        retail[1],
        mobile[0],
        mobile[1],
        [],
    )
    return core, None, None, {}, {}


def parse_pdf(
    data: bytes,
    year: int,
    month: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    float,
    float,
    float | None,
    float | None,
    float | None,
    float | None,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
] | None:
    parsed = _parse_pdf_with_fiscal(data, year, month, log=log)
    return parsed[0] if parsed is not None else None


def _parse_local_file_with_fiscal(
    local_dir: str,
    year: int,
    month: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
):
    month_token = f"{month:02d}{year % 100:02d}"
    month_name = date(year, month, 1).strftime("%b").lower()
    for entry in sorted(os.listdir(local_dir)):
        if not entry.lower().endswith(".pdf"):
            continue
        lower = entry.lower()
        if month_token in lower or (month_name in lower and str(year) in lower):
            with open(os.path.join(local_dir, entry), "rb") as file_handle:
                return _parse_pdf_with_fiscal(file_handle.read(), year, month, log=log)
    return None


def parse_local_file(
    local_dir: str,
    year: int,
    month: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    float,
    float,
    float | None,
    float | None,
    float | None,
    float | None,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
] | None:
    parsed = _parse_local_file_with_fiscal(local_dir, year, month, log=log)
    return parsed[0] if parsed is not None else None


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
    """Fetch and parse Missouri monthly sports wagering results."""
    if pdfplumber is None:
        raise RuntimeError("pdfplumber is required to fetch Missouri sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    live_urls: dict[str, str] | None = None
    results: list[StateMonthResult] = []
    for mdate in target_dates:
        year = mdate.year
        month = mdate.month
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(year, month)
        try:
            logger(f"INFO processing {label}")
            parsed_fiscal = _parse_local_file_with_fiscal(local_dir, year, month, log=logger) if local_dir else None
            chosen_url: str | None = None
            if parsed_fiscal:
                logger(f"INFO parsed local PDF for {label}")
                chosen_url = build_pdf_url(year, month)
            if parsed_fiscal is None:
                path = cache_path(cache_dir, f"missouri_{year}_{month:02d}.pdf")
                cached = bool(path and os.path.isfile(path))
                url: str | None = None
                if offline:
                    if not cached:
                        logger(f"INFO offline cache miss for missouri_{year}_{month:02d}.pdf")
                        continue
                    url = build_pdf_url(year, month)
                else:
                    if live_urls is None and (not cached or report is not None):
                        live_urls = scrape_pdf_urls(report=report, log=logger)
                    if live_urls:
                        url = live_urls.get(label)
                        if not url and not cached:
                            logger(f"INFO {label}: not yet published on MGC SW index; skipping")
                            if report is not None:
                                report.add_probe(f"{label}:summary", "(none)", reason="not-on-index")
                            continue
                    else:
                        url = build_pdf_url(year, month)
                        if not cached:
                            logger(f"INFO {label}: scraped URL missing; falling back to constructed URL")
                timestamp = datetime(year, month, 1).strftime("%Y%m")
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda u=url, ts=timestamp: fetch_with_wayback_fallback(
                        u, timestamp_yyyymm=ts, signature_check=b"%PDF", log=logger,
                    ),
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {label}",
                    fetch_success_message=f"INFO {label}: downloaded from {url}",
                    fetch_error_message=f"{label}: failed to download {url}",
                    validate=lambda payload: payload.startswith(b"%PDF"),
                    offline=offline,
                )
                if report is not None and url:
                    report.add_probe(
                        f"{label}:summary",
                        url,
                        status="ok" if data else "fail",
                        reason="downloaded" if data else "fetch-failed",
                    )
                if data:
                    parsed_fiscal = _parse_pdf_with_fiscal(data, year, month, log=logger)
                    chosen_url = url
            if parsed_fiscal is None:
                log_skip(logger, label, "no parseable MGC PDF")
                continue
            core, state_gross_rev, state_tax, op_taxes, op_gross = parsed_fiscal
            handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details = core
            enriched_details = [
                (
                    *op_tuple,
                    op_gross.get(_fiscal_operator_key(op_tuple[0])),
                    None,
                    op_taxes.get(_fiscal_operator_key(op_tuple[0])),
                )
                for op_tuple in details
            ]
            operator_details = build_operator_results(iso_month, enriched_details)
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
                details=operator_details,
                gross_revenue=state_gross_rev,
                tax=state_tax,
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
                f"hold={(item.hold or 0.0):.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
