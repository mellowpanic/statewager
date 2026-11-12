"""Kansas sports wagering regulatory fetcher and parser (Kansas Lottery)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import os
import re
import shutil
import time
from typing import Callable
from urllib.parse import urljoin

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from ..cache import cache_path, load_cache, load_cached_or_fetch
from ..discover import MONTH_NAMES, DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_with_wayback_fallback
from ..model import (
    StateMonthResult,
    build_state_logger,
    format_iso_month,
    hold_pct,
    log_skip,
    resolve_fetch_months,
    validate_result,
)
from ..parse import extract_pdf_text, extract_pdf_text_with_ocr, parse_dollar

STATE_NAME = "Kansas"
LAUNCH_DATE: date | None = date(2022, 9, 1)
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = False

USER_AGENT = "Mozilla/5.0"
KS_LOTTERY_BASE = "https://www.kslottery.gov"
KS_SUMMARY_PAGE = f"{KS_LOTTERY_BASE}/publications/sports-monthly-revenues/"
KS_DETAIL_PAGE = f"{KS_LOTTERY_BASE}/publications/sports-monthly-detail-breakdown/"

SUMMARY_PDF_HASHES = {
    "Feb 2025": "cyebwaej",
    "Mar 2025": "2oulf4jy",
    "Apr 2025": "jcgjgxce",
    "May 2025": "ujwdihwg",
    "Jun 2025": "5g2depr4",
    "Jul 2025": "5npnomwo",
    "Aug 2025": "n33pqqbc",
    "Sep 2025": "pujlo2zb",
    "Oct 2025": "njwbmp5r",
    "Nov 2025": "xyghs3ye",
    "Dec 2025": "lfok2nd1",
    "Jan 2026": "jswjvfff",
    "Feb 2026": "04weixyd",
    "Mar 2026": "oy5plg12",
}

DETAIL_PDF_HASHES = {
    "Jan 2025": "ry5ioquv",
    "Feb 2025": "e5ettpde",
    "Mar 2025": "ev2d1s44",
    "Apr 2025": "avjpho4w",
    "May 2025": "e0kjwll3",
    "Jun 2025": "fbnlfu25",
    "Jul 2025": "qllpp5uu",
    "Aug 2025": "vqlhksbj",
    "Sep 2025": "4kfhidxp",
    "Oct 2025": "edld13cs",
    "Nov 2025": "ky1mf2pk",
    "Dec 2025": "n23boyce",
    "Jan 2026": "djlwmgdy",
    "Feb 2026": "qetawn52",
    "Mar 2026": "fwijvoam",
}

_DEFAULT_LOG = build_state_logger("KS")

SUMMARY_PDF_NAME_RE = re.compile(r"sports-wagering-monthly-revenue-(\d{4})-(\d{2})\.pdf", re.IGNORECASE)
DETAIL_PDF_NAME_RE = re.compile(r"sports-wagering-monthly-detail-(\d{4})-(\d{2})\.pdf", re.IGNORECASE)
TEXT_MONTH_RE = re.compile(
    r"\b("
    + "|".join(sorted(MONTH_NAMES.keys(), key=len, reverse=True))
    + r")\b(?:\s+report)?(?:\s*\(pdf\))?[\s,\-]+(20\d{2})",
    re.IGNORECASE,
)

AMOUNT_TOKEN_RE = re.compile(r"\(?\$?[\d,]+(?:\.\d+)?\)?|-")
LABEL_KEYWORDS = {
    "subtotal - retail": "retail",
    "subtotal - online": "mobile",
    "totals": "total",
}

DETAIL_IMAGE_OVERRIDES = {
    "Aug 2025": (
        189802925.0,
        12472230.0,
        2994610.0,
        549722.0,
        186808315.0,
        11922508.0,
    ),
    "Oct 2025": (
        306043913.0,
        12687085.0,
        6523286.0,
        675234.0,
        299520627.0,
        12011851.0,
    ),
}

DETAIL_MIN_USABLE_WORDS = 50


def scrape_pdf_urls(
    page_url: str,
    pdf_name_re: re.Pattern[str],
    kind: str,
    *,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[str, str]:
    log(f"INFO scraping kslottery.gov for {kind} PDF links...")
    links = crawl_anchors(page_url, log=log)
    if not links:
        log(f"WARN could not fetch KS Lottery {kind} page; falling back to hardcoded hashes")
        return {}
    if report is not None:
        report.add_index(page_url, anchor_count=len(links))
    urls: dict[str, str] = {}
    for link in links:
        href = unescape(link.get("href", "").strip())
        if not href.lower().endswith(".pdf"):
            if report is not None and href:
                report.add_classification(
                    href, title=link.get("title", ""), text=link.get("text", ""), reason="not-pdf",
                )
            continue
        match = pdf_name_re.search(href)
        if match:
            year, month_num = int(match.group(1)), int(match.group(2))
            label = datetime(year, month_num, 1).strftime("%b %Y")
        else:
            meta = " ".join(unescape(link.get(key, "")) for key in ("title", "text"))
            text_match = TEXT_MONTH_RE.search(meta)
            if text_match:
                month_num = MONTH_NAMES[text_match.group(1).lower()]
                year = int(text_match.group(2))
                label = datetime(year, month_num, 1).strftime("%b %Y")
            else:
                combined = " ".join([href, meta])
                label = extract_month_label(combined)
                if not label:
                    if report is not None:
                        report.add_classification(
                            href,
                            title=link.get("title", ""),
                            text=link.get("text", ""),
                            reason="no-month-token",
                        )
                    continue
        full_url = urljoin(KS_LOTTERY_BASE, href)
        urls[label] = full_url
        if report is not None:
            report.add_anchor(f"{label}:{kind}", full_url)
    log(f"INFO scraped {len(urls)} {kind} PDF links from kslottery.gov")
    return urls


def build_download_url(
    label: str,
    live_urls: dict[str, str],
    *,
    kind: str,
    hashes: dict[str, str],
    filename_prefix: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> str | None:
    if label in live_urls:
        return live_urls[label]
    hash_value = hashes.get(label)
    if not hash_value:
        return None
    month = datetime.strptime(label, "%b %Y")
    log(f"INFO using hardcoded {kind} hash for {label}")
    return f"{KS_LOTTERY_BASE}/media/{hash_value}/{filename_prefix}-{month:%Y-%m}.pdf"


def normalize_pdf_label(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _extract_handle_and_revenue_from_line(line: str) -> tuple[float | None, float | None]:
    tokens = AMOUNT_TOKEN_RE.findall(line)
    if "subtotal -" in normalize_pdf_label(line) and tokens and tokens[0] == "-":
        tokens = tokens[1:]
    if len(tokens) >= 6:
        handle = parse_dollar(tokens[0])
        revenue = parse_dollar(tokens[5])
        return handle, revenue
    if len(tokens) >= 2:
        handle = parse_dollar(tokens[0])
        revenue = parse_dollar(tokens[1])
        return handle, revenue
    return None, None


def _is_label_only_line(normalized: str, raw_line: str) -> bool:
    return normalized in LABEL_KEYWORDS and not re.search(r"\d", raw_line)


def _map_label_block(
    extracted: dict[str, tuple[float, float]],
    lines: list[str],
    start_index: int,
) -> int:
    label_indexes: list[int] = []
    index = start_index
    while index < len(lines):
        normalized = normalize_pdf_label(lines[index])
        if not _is_label_only_line(normalized, lines[index]):
            break
        label_indexes.append(index)
        index += 1
    amount_lines: list[str] = []
    probe = start_index - 1
    while probe >= 0 and len(amount_lines) < len(label_indexes):
        candidate = lines[probe]
        normalized = normalize_pdf_label(candidate)
        if (
            normalized not in LABEL_KEYWORDS
            and len(AMOUNT_TOKEN_RE.findall(candidate)) >= 2
            and not re.search(r"[A-Za-z]", candidate)
        ):
            amount_lines.append(candidate)
        probe -= 1
    amount_lines.reverse()
    for label_index, amount_line in zip(label_indexes, amount_lines):
        target = LABEL_KEYWORDS[normalize_pdf_label(lines[label_index])]
        handle, revenue = _extract_handle_and_revenue_from_line(amount_line)
        if handle is not None and revenue is not None:
            extracted[target] = (handle, revenue)
    return index


def _extract_from_text(text: str) -> tuple[float, float, float | None, float | None, float | None, float | None] | None:
    extracted: dict[str, tuple[float, float]] = {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    index = 0
    while index < len(lines):
        line = lines[index]
        normalized = normalize_pdf_label(line)
        if _is_label_only_line(normalized, line):
            index = _map_label_block(extracted, lines, index)
            continue
        for keyword, target in LABEL_KEYWORDS.items():
            if keyword not in normalized:
                continue
            handle, revenue = _extract_handle_and_revenue_from_line(line)
            if handle is not None and revenue is not None:
                extracted[target] = (handle, revenue)
            break
        index += 1
    retail = extracted.get("retail")
    mobile = extracted.get("mobile")
    total = extracted.get("total")
    if total is None and retail and mobile:
        total = (retail[0] + mobile[0], retail[1] + mobile[1])
    if total is None:
        return None
    if total[0] is None or total[1] is None:
        return None
    return (
        total[0],
        total[1],
        retail[0] if retail else None,
        retail[1] if retail else None,
        mobile[0] if mobile else None,
        mobile[1] if mobile else None,
    )


def _looks_fragmented_text(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    single_char_lines = sum(1 for line in lines if len(line) == 1)
    spaced_fragments = sum(1 for line in lines if re.search(r"(?:\b\w\s+){3,}\w\b", line))
    return single_char_lines >= 10 or spaced_fragments >= 3


def _extract_pdftotext_raw(data: bytes, first_page: int = 1, last_page: int = 1) -> str | None:
    text = extract_pdf_text(data, first_page=first_page, last_page=last_page, raw=True)
    return text.split("\f", 1)[0] if text else None


def _extract_pdf_label(text: str) -> str | None:
    month_match = TEXT_MONTH_RE.search(text)
    if not month_match:
        return None
    month_num = MONTH_NAMES[month_match.group(1).lower()]
    year = int(month_match.group(2))
    return datetime(year, month_num, 1).strftime("%b %Y")


def _count_usable_words(text: str) -> int:
    return len(re.findall(r"\b[\w$(),.-]+\b", text or ""))


def parse_summary_pdf(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[tuple[float, float], str] | None:
    fallback_text = _extract_pdftotext_raw(data)
    if fallback_text:
        extracted = _extract_from_text(fallback_text)
        if extracted is not None:
            return (extracted[0], extracted[1]), "pdftotext_raw"
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if not pdf.pages:
                    log("WARN Kansas PDF contained no pages")
                    return None
                page = pdf.pages[0]
                total = None
                for table in page.extract_tables():
                    for row in table:
                        label = normalize_pdf_label(row[0] if row else "")
                        handle = parse_dollar(row[2]) if len(row) > 2 else None
                        revenue = parse_dollar(row[3]) if len(row) > 3 else None
                        if handle is None or revenue is None:
                            continue
                        if label == "totals":
                            total = (handle, revenue)
                if total is not None:
                    return total, "pdfplumber_table"
                text = page.extract_text() or ""
        except Exception as exc:
            log(f"WARN could not parse Kansas PDF with pdfplumber: {exc}")
        else:
            if not _looks_fragmented_text(text):
                extracted = _extract_from_text(text)
                if extracted is not None:
                    return (extracted[0], extracted[1]), "pdfplumber_text"
    log("WARN Kansas PDF parse returned no totals from table or text")
    return None


def parse_detail_pdf(
    data: bytes,
    month_label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[tuple[float, float, float | None, float | None, float | None, float | None], str] | None:
    """Parse Kansas detail splits."""
    fallback_text = _extract_pdftotext_raw(data)
    label = _extract_pdf_label(fallback_text or "") or month_label
    usable_word_counts: list[int] = []
    if fallback_text:
        usable_word_counts.append(_count_usable_words(fallback_text))
        extracted = _extract_from_text(fallback_text)
        if extracted is not None:
            return extracted, "pdftotext_raw"
    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if not pdf.pages:
                    log("WARN Kansas detail PDF contained no pages")
                    return None
                text = pdf.pages[0].extract_text() or ""
        except Exception as exc:
            log(f"WARN could not parse Kansas detail PDF with pdfplumber: {exc}")
        else:
            usable_word_counts.append(_count_usable_words(text))
            if not _looks_fragmented_text(text):
                extracted = _extract_from_text(text)
                if extracted is not None:
                    return extracted, "pdfplumber_text"
            if label is None:
                label = _extract_pdf_label(text)
    if label in DETAIL_IMAGE_OVERRIDES:
        return DETAIL_IMAGE_OVERRIDES[label], "image_override"
    max_usable_words = max(usable_word_counts, default=0)
    if max_usable_words < DETAIL_MIN_USABLE_WORDS:
        log(f"INFO Kansas detail PDF for {month_label} appears image-based; trying OCR")
        ocr_text = extract_pdf_text_with_ocr(data, first_page=1, last_page=1, log=log)
        if ocr_text:
            extracted = _extract_from_text(ocr_text)
            if extracted is not None:
                return extracted, "ocr"
            log(f"WARN Kansas detail OCR for {month_label} ran but parser found no rows")
        else:
            log(f"WARN Kansas detail PDF for {month_label} needs OCR but tesseract unavailable")
        return None
    log("WARN Kansas detail PDF parse returned no split rows")
    return None


def parse_local_file(
    local_dir: str,
    month: int,
    year: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[tuple[float, float], str] | None:
    full_month = date(year, month, 1).strftime("%B").lower()
    short_month = date(year, month, 1).strftime("%b").lower()
    for entry in sorted(os.listdir(local_dir)):
        if not entry.lower().endswith(".pdf"):
            continue
        lower = entry.lower()
        if str(year) in lower and (full_month in lower or short_month in lower):
            with open(os.path.join(local_dir, entry), "rb") as file_handle:
                return parse_summary_pdf(file_handle.read(), log=log)
    return None


def _try_fresh_cache(
    path: str | None,
    *,
    ttl_seconds: float | None,
    offline: bool,
    hit_message: str,
    log: Callable[[str], None],
) -> bytes | None:
    if not path or not os.path.isfile(path):
        return None
    if not offline and ttl_seconds is not None:
        try:
            age = time.time() - os.path.getmtime(path)
        except OSError:
            age = 0.0
        if age > ttl_seconds:
            return None
    data = load_cache(path)
    if data and data.startswith(b"%PDF"):
        log(hit_message)
        return data
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
    """Fetch and parse Kansas monthly sports wagering results."""
    if pdfplumber is None and not shutil.which("pdftotext"):
        raise RuntimeError("pdfplumber or pdftotext is required to fetch Kansas sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    current_year = date.today().year
    summary_urls: dict[str, str] | None = None
    detail_urls: dict[str, str] | None = None
    results: list[StateMonthResult] = []

    for mdate in target_dates:
        month = mdate.month
        year = mdate.year
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(year, month)
        ttl_seconds = 7 * 86400 if year >= current_year else None
        try:
            logger(f"INFO processing {label}")
            summary_parsed = None
            summary_strategy = None
            detail_parsed = None
            chosen_url: str | None = None

            if local_dir:
                summary_parsed = parse_local_file(local_dir, month, year, log=logger)
                if summary_parsed:
                    logger(f"INFO {label}: using local file")
                    chosen_url = KS_SUMMARY_PAGE

            if summary_parsed is None:
                summary_path = cache_path(cache_dir, f"kansas_{year}_{month:02d}.pdf")
                summary_data = _try_fresh_cache(
                    summary_path,
                    ttl_seconds=ttl_seconds,
                    offline=offline,
                    hit_message=f"INFO using cached file for {label}",
                    log=logger,
                )
                url: str | None = None
                if summary_data is None and not offline:
                    if summary_urls is None:
                        summary_urls = scrape_pdf_urls(
                            KS_SUMMARY_PAGE, SUMMARY_PDF_NAME_RE, "summary", report=report, log=logger,
                        )
                    url = build_download_url(
                        label,
                        summary_urls,
                        kind="summary",
                        hashes=SUMMARY_PDF_HASHES,
                        filename_prefix="sports-wagering-monthly-revenue",
                        log=logger,
                    )
                    if not url:
                        logger(f"WARN {label}: no discovered or fallback PDF URL")
                        if report is not None:
                            report.add_probe(f"{label}:summary", "(none)", reason="no-url")
                    else:
                        summary_ts = datetime.strptime(label, "%b %Y").strftime("%Y%m")
                        summary_data = load_cached_or_fetch(
                            cache_file=summary_path,
                            fetcher=lambda u=url, ts=summary_ts: fetch_with_wayback_fallback(
                                u, timestamp_yyyymm=ts, signature_check=b"%PDF", log=logger,
                            ),
                            log=logger,
                            cache_hit_message=f"INFO using cached file for {label}",
                            fetch_success_message=f"INFO {label}: downloaded from {url}",
                            fetch_error_message=f"could not download Kansas PDF for {label}",
                            validate=lambda payload: payload.startswith(b"%PDF"),
                            cache_ttl_seconds=ttl_seconds,
                            offline=offline,
                        )
                        if report is not None:
                            report.add_probe(
                                f"{label}:summary",
                                url,
                                status="ok" if summary_data else "fail",
                                reason="downloaded" if summary_data else "fetch-failed",
                            )
                else:
                    url = build_download_url(
                        label,
                        summary_urls or {},
                        kind="summary",
                        hashes=SUMMARY_PDF_HASHES,
                        filename_prefix="sports-wagering-monthly-revenue",
                        log=lambda _m: None,
                    )
                if summary_data:
                    summary_parsed = parse_summary_pdf(summary_data, log=logger)
                    chosen_url = url or KS_SUMMARY_PAGE
                    if summary_parsed is None:
                        logger(f"WARN {label}: PDF parse returned no totals")

            if summary_parsed is None:
                log_skip(logger, label, "no matching PDF or parseable download")
                continue

            (handle, revenue), summary_strategy = summary_parsed
            retail_handle = retail_revenue = mobile_handle = mobile_revenue = None
            detail_path = cache_path(cache_dir, f"kansas_detail_{year}_{month:02d}.pdf")
            detail_data = _try_fresh_cache(
                detail_path,
                ttl_seconds=ttl_seconds,
                offline=offline,
                hit_message=f"INFO using cached detail file for {label}",
                log=logger,
            )
            detail_url: str | None = None
            if detail_data is None and not offline:
                if detail_urls is None:
                    detail_urls = scrape_pdf_urls(
                        KS_DETAIL_PAGE, DETAIL_PDF_NAME_RE, "detail", report=report, log=logger,
                    )
                detail_url = build_download_url(
                    label,
                    detail_urls,
                    kind="detail",
                    hashes=DETAIL_PDF_HASHES,
                    filename_prefix="sports-wagering-monthly-detail",
                    log=logger,
                )
                if detail_url:
                    detail_ts = datetime.strptime(label, "%b %Y").strftime("%Y%m")
                    detail_data = load_cached_or_fetch(
                        cache_file=detail_path,
                        fetcher=lambda u=detail_url, ts=detail_ts: fetch_with_wayback_fallback(
                            u, timestamp_yyyymm=ts, signature_check=b"%PDF", log=logger,
                        ),
                        log=logger,
                        cache_hit_message=f"INFO using cached detail file for {label}",
                        fetch_success_message=f"INFO {label}: downloaded detail from {detail_url}",
                        fetch_error_message=f"could not download Kansas detail PDF for {label}",
                        validate=lambda payload: payload.startswith(b"%PDF"),
                        cache_ttl_seconds=ttl_seconds,
                        offline=offline,
                    )
                    if report is not None:
                        report.add_probe(
                            f"{label}:detail",
                            detail_url,
                            status="ok" if detail_data else "fail",
                            reason="downloaded" if detail_data else "fetch-failed",
                        )
                else:
                    logger(f"INFO {label}: no detail PDF discovered; leaving split columns blank")
            else:
                detail_url = build_download_url(
                    label,
                    detail_urls or {},
                    kind="detail",
                    hashes=DETAIL_PDF_HASHES,
                    filename_prefix="sports-wagering-monthly-detail",
                    log=lambda _m: None,
                )

            if detail_data:
                detail_parsed = parse_detail_pdf(detail_data, label, log=logger)
                if detail_parsed is not None:
                    (
                        detail_handle,
                        detail_revenue,
                        retail_handle,
                        retail_revenue,
                        mobile_handle,
                        mobile_revenue,
                    ), detail_strategy = detail_parsed
                    handle, revenue = detail_handle, detail_revenue
                    summary_strategy = f"detail_{detail_strategy}"
                    chosen_url = detail_url or chosen_url or KS_DETAIL_PAGE
                    logger(f"INFO {label}: parsed detail splits via {detail_strategy}")
                else:
                    logger(f"WARN {label}: detail PDF parse returned no split rows")

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
                    source_url=chosen_url,
                )
            )
            logger(
                f"INFO parsed {label} via {summary_strategy}: handle={handle:,.0f} revenue={revenue:,.0f} "
                f"retail_handle={retail_handle if retail_handle is not None else 'n/a'} "
                f"mobile_handle={mobile_handle if mobile_handle is not None else 'n/a'} hold={hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
