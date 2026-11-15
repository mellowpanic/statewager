"""Kansas sports wagering regulatory fetcher and parser (Kansas Lottery)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import os
import re
import shutil
from typing import Callable
from urllib.parse import urljoin

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from ..cache import cache_path, load_cached_or_fetch
from ..discover import MONTH_NAMES, TEXT_MONTH_RE, DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_with_wayback_fallback
from ..model import (
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
)
from ..parse import extract_pdf_text, extract_pdf_text_with_ocr, parse_dollar

STATE_NAME = "Kansas"
LAUNCH_DATE: date | None = date(2022, 9, 1)
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

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
_SPACED_FRAGMENTS_RE = re.compile(r"(?:\b\w\s+){3,}\w\b")
_USABLE_WORD_RE = re.compile(r"\b[\w$(),.-]+\b")

AMOUNT_TOKEN_RE = re.compile(r"[+\-\u2212]?\(?\$?[\d,]+(?:\.\d+)?\)?|(?<!\w)-(?![\w$(])")
_WS_RE = re.compile(r"\s+")
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

DETAIL_OPERATOR_OVERRIDES: dict[
    str,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
] = {
    "Aug 2025": [
        ("Boot Hill - DraftKings", 76436807.0, 5445804.0, 231476.0, 33351.0, 76205331.0, 5412453.0),
        ("Kansas Star - FanDuel", 59032691.0, 5010090.0, 760887.0, 123141.0, 58271804.0, 4886949.0),
        ("Hollywood - ESPNBet", 6931050.0, 909344.0, 1859461.0, 368936.0, 5071589.0, 540408.0),
        ("KS Crossing - Caesar's", 7955037.0, 29071.0, 142786.0, 24293.0, 7812251.0, 4778.0),
        ("Kansas Star - BET365", 5472548.0, 0.0, None, None, 5472548.0, 0.0),
        ("KS Crossing - BetMGM", 15234535.0, 0.0, None, None, 15234535.0, 0.0),
        ("KS Crossing - Fanatics", 18740258.0, 1077920.0, None, None, 18740258.0, 1077920.0),
    ],
    "Oct 2025": [
        ("Boot Hill - DraftKings", 129328220.0, 6149917.0, 317098.0, 39225.0, 129011122.0, 6110692.0),
        ("Kansas Star - FanDuel", 103241281.0, 3421281.0, 1649661.0, 103518.0, 101591620.0, 3317763.0),
        ("Hollywood - ESPNBet", 13882301.0, 732244.0, 4020004.0, 436564.0, 9862297.0, 295680.0),
        ("KS Crossing - Caesar's", 11029199.0, 142226.0, 536523.0, 95927.0, 10492676.0, 46299.0),
        ("Kansas Star - BET365", 10568402.0, 0.0, None, None, 10568402.0, 0.0),
        ("KS Crossing - BetMGM", 22350698.0, 1719729.0, None, None, 22350698.0, 1719729.0),
        ("KS Crossing - Fanatics", 15643811.0, 521689.0, None, None, 15643811.0, 521689.0),
    ],
}

_CASINO_PREFIXES: tuple[str, ...] = (
    "Boot Hill",
    "Kansas Star",
    "Hollywood",
    "KS Crossing",
    "Kansas Crossing",
)
_PROVIDER_SPLIT_RE = re.compile(r"^([A-Za-z][A-Za-z0-9' ]*?)\s+(?=[+\-\u2212]?\(?\$?\d|-(?:\s|$))")
_BET365_SPACE_RE = re.compile(r"\bBET\s+365\b", re.IGNORECASE)

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
            if not (1 <= month_num <= 12):
                continue
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
    return _WS_RE.sub(" ", str(value or "").strip()).lower()


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


_HAS_DIGIT_RE = re.compile(r"\d")
_HAS_ALPHA_RE = re.compile(r"[A-Za-z]")


def _is_label_only_line(normalized: str, raw_line: str) -> bool:
    return normalized in LABEL_KEYWORDS and not _HAS_DIGIT_RE.search(raw_line)


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
            and not _HAS_ALPHA_RE.search(candidate)
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
    spaced_fragments = sum(1 for line in lines if _SPACED_FRAGMENTS_RE.search(line))
    return single_char_lines >= 10 or spaced_fragments >= 3


def _extract_pdftotext_raw(data: bytes, first_page: int = 1, last_page: int = 1) -> str | None:
    text = extract_pdf_text(data, first_page=first_page, last_page=last_page, raw=True)
    return text.split("\f", 1)[0] if text else None


def _extract_pdf_label(text: str) -> str | None:
    month_match = TEXT_MONTH_RE.search(text)
    if not month_match:
        return None
    month_num = MONTH_NAMES.get(month_match.group(1).lower())
    year = int(month_match.group(2))
    if not month_num or not (1900 <= year <= 2100):
        return None
    return datetime(year, month_num, 1).strftime("%b %Y")


def _count_usable_words(text: str) -> int:
    return len(_USABLE_WORD_RE.findall(text or ""))


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


def _parse_ks_cell_amount(value: object) -> float:
    text = str(value or "").strip()
    if not text or text == "-":
        return 0.0
    parsed = parse_dollar(text)
    return 0.0 if parsed is None else parsed


def _record_ks_operator_row(
    merged: dict[str, list[float | None]],
    order: list[str],
    casino: str,
    provider: str,
    section: str,
    handle: float,
    revenue: float,
) -> None:
    key = f"{casino} - {provider}"
    if key not in merged:
        merged[key] = [None, None, None, None]
        order.append(key)
    bucket = merged[key]
    if section == "retail":
        bucket[0] = (bucket[0] or 0.0) + handle
        bucket[1] = (bucket[1] or 0.0) + revenue
    else:
        bucket[2] = (bucket[2] or 0.0) + handle
        bucket[3] = (bucket[3] or 0.0) + revenue


def parse_detail_operators(
    data: bytes,
    month_label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> list[tuple[str, float, float, float | None, float | None, float | None, float | None]]:
    """Extract per-(casino, provider) retail + online wagers and net revenues from Kansas detail PDFs."""
    fallback_text = _extract_pdftotext_raw(data)
    label = _extract_pdf_label(fallback_text or "") or month_label
    if label in DETAIL_OPERATOR_OVERRIDES:
        return list(DETAIL_OPERATOR_OVERRIDES[label])

    merged: dict[str, list[float | None]] = {}
    order: list[str] = []

    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if pdf.pages:
                    tables = pdf.pages[0].extract_tables()
                    rows = [r for t in tables for r in t if any(c and str(c).strip() for c in r)]
                    if len(rows) >= 10:
                        section = "retail"
                        for row in rows:
                            c0 = normalize_pdf_label(row[0] if len(row) > 0 else "")
                            c2_low = str(row[2] if len(row) > 2 else "").lower()
                            if "casino" in c0 or "settled" in c2_low:
                                continue
                            if "subtotal - retail" in c0:
                                section = "online"
                                continue
                            if "subtotal - online" in c0 or c0 == "totals":
                                break
                            casino = _WS_RE.sub(" ", str(row[0] or "").strip())
                            provider = _WS_RE.sub(" ", str(row[1] if len(row) > 1 else "").strip())
                            if not casino or not provider:
                                continue
                            handle = _parse_ks_cell_amount(row[2] if len(row) > 2 else None)
                            revenue = _parse_ks_cell_amount(row[7] if len(row) > 7 else None)
                            _record_ks_operator_row(merged, order, casino, provider, section, handle, revenue)
        except Exception as exc:
            log(f"WARN could not parse Kansas operator table with pdfplumber for {month_label}: {exc}")

    if not merged and fallback_text:
        section = "retail"
        seen_in_retail: set[str] = set()
        for raw_line in fallback_text.splitlines():
            collapsed = _WS_RE.sub(" ", raw_line).strip()
            collapsed = _BET365_SPACE_RE.sub("BET365", collapsed)
            low = collapsed.lower()
            if "subtotal - retail" in low:
                section = "online"
                continue
            if "subtotal - online" in low or low.startswith("totals"):
                break
            casino = next((prefix for prefix in _CASINO_PREFIXES if collapsed.startswith(prefix + " ")), None)
            if not casino:
                continue
            rest = collapsed[len(casino):].strip()
            prov_match = _PROVIDER_SPLIT_RE.match(rest)
            if not prov_match:
                continue
            provider = prov_match.group(1).strip()
            amt_part = rest[prov_match.end():]
            tokens = AMOUNT_TOKEN_RE.findall(amt_part)
            if len(tokens) < 6:
                continue
            key = f"{casino} - {provider}"
            if section == "retail" and key in seen_in_retail:
                section = "online"
            if section == "retail":
                seen_in_retail.add(key)
            handle = _parse_ks_cell_amount(tokens[0])
            revenue = _parse_ks_cell_amount(tokens[5])
            _record_ks_operator_row(merged, order, casino, provider, section, handle, revenue)

    output: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
    for key in order:
        rh, rr, mh, mr = merged[key]
        output.append(
            (
                key,
                (rh or 0.0) + (mh or 0.0),
                (rr or 0.0) + (mr or 0.0),
                rh,
                rr,
                mh,
                mr,
            )
        )
    return output


def _ks_fiscal_key(casino: str, provider: str) -> str:
    clean_c = _WS_RE.sub(" ", casino).strip().lower()
    clean_p = _BET365_SPACE_RE.sub("BET365", _WS_RE.sub(" ", provider).strip()).lower()
    return f"{clean_c} - {clean_p}"


def parse_summary_fiscal(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[float | None, dict[str, float]]:
    """Extract Monthly Summary ``(statewide_tax, {canonical_op_key: tax})`` from Kansas summary PDFs."""
    state_tax: float | None = None
    op_taxes: dict[str, float] = {}

    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if pdf.pages:
                    tables = pdf.pages[0].extract_tables()
                    if tables:
                        for row in tables[0]:
                            c0 = normalize_pdf_label(row[0] if len(row) > 0 else "")
                            if not c0 or "casino" in c0:
                                continue
                            if "fiscal year" in c0:
                                break
                            tax_val = parse_dollar(row[4]) if len(row) > 4 else None
                            if tax_val is None:
                                continue
                            if c0 == "totals":
                                state_tax = tax_val
                                break
                            if "subtotal -" in c0:
                                continue
                            casino = _WS_RE.sub(" ", str(row[0] or "").strip())
                            provider = _WS_RE.sub(" ", str(row[1] if len(row) > 1 else "").strip())
                            if casino and provider:
                                k = _ks_fiscal_key(casino, provider)
                                op_taxes[k] = op_taxes.get(k, 0.0) + tax_val
        except Exception as exc:
            log(f"WARN could not parse Kansas summary tax table for fiscal extraction: {exc}")

    if state_tax is None:
        fallback_text = _extract_pdftotext_raw(data)
        if fallback_text:
            for raw_line in fallback_text.splitlines():
                collapsed = _BET365_SPACE_RE.sub("BET365", _WS_RE.sub(" ", raw_line).strip())
                low = collapsed.lower()
                if "fiscal year" in low:
                    break
                if low.startswith("totals"):
                    tokens = AMOUNT_TOKEN_RE.findall(collapsed)
                    if len(tokens) >= 3:
                        state_tax = _parse_ks_cell_amount(tokens[2])
                    break
                if "subtotal -" in low:
                    continue
                casino = next((p for p in _CASINO_PREFIXES if collapsed.startswith(p + " ")), None)
                if casino:
                    rest = collapsed[len(casino):].strip()
                    prov_match = _PROVIDER_SPLIT_RE.match(rest)
                    if prov_match:
                        provider = prov_match.group(1).strip()
                        tokens = AMOUNT_TOKEN_RE.findall(rest[prov_match.end():])
                        if len(tokens) >= 3:
                            k = _ks_fiscal_key(casino, provider)
                            op_taxes[k] = op_taxes.get(k, 0.0) + _parse_ks_cell_amount(tokens[2])
    return state_tax, op_taxes


def _add_ks_fiscal(
    bucket_map: dict[str, list[float]],
    key: str,
    gross_rev: float,
    promo: float,
    tax: float,
) -> None:
    cur = bucket_map.setdefault(key, [0.0, 0.0, 0.0])
    cur[0] += gross_rev
    cur[1] += promo
    cur[2] += tax


def parse_detail_fiscal(
    data: bytes,
    month_label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    tuple[float | None, float | None, float | None],
    dict[str, tuple[float | None, float | None, float | None]],
]:
    """Extract ``((gross_rev, promo, tax), {canonical_op_key: (gross_rev, promo, tax)})`` from Kansas detail PDFs."""
    state_fiscal: tuple[float | None, float | None, float | None] = (None, None, None)
    op_buckets: dict[str, list[float]] = {}

    if pdfplumber is not None:
        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                if pdf.pages:
                    tables = pdf.pages[0].extract_tables()
                    rows = [r for t in tables for r in t if any(c and str(c).strip() for c in r)]
                    w_col, p_col, pr_col, tx_col = 2, 3, 4, 9
                    for row in rows:
                        if len(row) >= 10 and "casino" in normalize_pdf_label(row[0] or ""):
                            for idx, cell in enumerate(row):
                                htxt = normalize_pdf_label(cell or "")
                                if "settled" in htxt and "wager" in htxt:
                                    w_col = idx
                                elif "prizes" in htxt:
                                    p_col = idx
                                elif "promotional" in htxt:
                                    pr_col = idx
                                elif "state share" in htxt:
                                    tx_col = idx
                            continue
                        if len(row) <= max(w_col, p_col, pr_col, tx_col):
                            continue
                        c0 = normalize_pdf_label(row[0] or "")
                        if "subtotal -" in c0:
                            continue
                        wagers = _parse_ks_cell_amount(row[w_col])
                        prizes = _parse_ks_cell_amount(row[p_col])
                        gross_rev = wagers - abs(prizes)
                        promo = abs(_parse_ks_cell_amount(row[pr_col]))
                        tax = _parse_ks_cell_amount(row[tx_col])
                        if c0 == "totals":
                            state_fiscal = (gross_rev, promo, tax)
                            break
                        casino = _WS_RE.sub(" ", str(row[0] or "").strip())
                        provider = _WS_RE.sub(" ", str(row[1] or "").strip())
                        if casino and provider:
                            _add_ks_fiscal(op_buckets, _ks_fiscal_key(casino, provider), gross_rev, promo, tax)
        except Exception as exc:
            log(f"WARN could not parse Kansas detail fiscal table for {month_label}: {exc}")

    if state_fiscal == (None, None, None):
        fallback_text = _extract_pdftotext_raw(data)
        if fallback_text:
            for raw_line in fallback_text.splitlines():
                collapsed = _BET365_SPACE_RE.sub("BET365", _WS_RE.sub(" ", raw_line).strip())
                low = collapsed.lower()
                if "subtotal -" in low:
                    continue
                if low.startswith("totals"):
                    tokens = AMOUNT_TOKEN_RE.findall(collapsed)
                    if len(tokens) >= 7:
                        wagers = _parse_ks_cell_amount(tokens[0])
                        prizes = _parse_ks_cell_amount(tokens[1])
                        promo = abs(_parse_ks_cell_amount(tokens[2]))
                        tax = _parse_ks_cell_amount(tokens[-1])
                        state_fiscal = (wagers - abs(prizes), promo, tax)
                    break
                casino = next((p for p in _CASINO_PREFIXES if collapsed.startswith(p + " ")), None)
                if casino:
                    rest = collapsed[len(casino):].strip()
                    prov_match = _PROVIDER_SPLIT_RE.match(rest)
                    if prov_match:
                        provider = prov_match.group(1).strip()
                        tokens = AMOUNT_TOKEN_RE.findall(rest[prov_match.end():])
                        if len(tokens) >= 7:
                            wagers = _parse_ks_cell_amount(tokens[0])
                            prizes = _parse_ks_cell_amount(tokens[1])
                            promo = abs(_parse_ks_cell_amount(tokens[2]))
                            tax = _parse_ks_cell_amount(tokens[-1])
                            _add_ks_fiscal(
                                op_buckets, _ks_fiscal_key(casino, provider), wagers - abs(prizes), promo, tax,
                            )

    op_fiscal = {k: (v[0], v[1], v[2]) for k, v in op_buckets.items()}
    return state_fiscal, op_fiscal


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
        ts_ym = f"{year}{month:02d}"
        ttl_seconds = 7 * 86400 if year >= current_year else None
        try:
            logger(f"INFO processing {label}")
            summary_parsed = None
            summary_strategy = None
            detail_parsed = None
            chosen_url: str | None = None

            summary_data_bytes: bytes | None = None
            if local_dir:
                summary_parsed = parse_local_file(local_dir, month, year, log=logger)
                if summary_parsed:
                    logger(f"INFO {label}: using local file")
                    chosen_url = KS_SUMMARY_PAGE

            if summary_parsed is None:
                summary_path = cache_path(cache_dir, f"kansas_{year}_{month:02d}.pdf")
                fetched_summary_url: list[str] = []

                def _fetch_summary(
                    lbl: str = label,
                    ts: str = ts_ym,
                    out_url: list[str] = fetched_summary_url,
                ) -> bytes | None:
                    nonlocal summary_urls
                    if summary_urls is None:
                        summary_urls = scrape_pdf_urls(
                            KS_SUMMARY_PAGE, SUMMARY_PDF_NAME_RE, "summary", report=report, log=logger,
                        )
                    target_url = build_download_url(
                        lbl,
                        summary_urls,
                        kind="summary",
                        hashes=SUMMARY_PDF_HASHES,
                        filename_prefix="sports-wagering-monthly-revenue",
                        log=logger,
                    )
                    if not target_url:
                        logger(f"WARN {lbl}: no discovered or fallback PDF URL")
                        if report is not None:
                            report.add_probe(f"{lbl}:summary", "(none)", reason="no-url")
                        return None
                    out_url[:] = [target_url]
                    downloaded = fetch_with_wayback_fallback(
                        target_url, timestamp_yyyymm=ts, signature_check=b"%PDF", log=logger,
                    )
                    if report is not None:
                        report.add_probe(
                            f"{lbl}:summary",
                            target_url,
                            status="ok" if downloaded else "fail",
                            reason="downloaded" if downloaded else "fetch-failed",
                        )
                    return downloaded

                summary_data = load_cached_or_fetch(
                    cache_file=summary_path,
                    fetcher=_fetch_summary,
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {label}",
                    fetch_success_message=f"INFO {label}: downloaded summary PDF",
                    fetch_error_message=f"could not download Kansas PDF for {label}",
                    validate=lambda payload: payload.startswith(b"%PDF"),
                    cache_ttl_seconds=ttl_seconds,
                    offline=offline,
                )
                url = (
                    fetched_summary_url[0]
                    if fetched_summary_url
                    else build_download_url(
                        label,
                        summary_urls or {},
                        kind="summary",
                        hashes=SUMMARY_PDF_HASHES,
                        filename_prefix="sports-wagering-monthly-revenue",
                        log=lambda _m: None,
                    )
                )
                if summary_data:
                    summary_data_bytes = summary_data
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
            fetched_detail_url: list[str] = []

            def _fetch_detail(
                lbl: str = label,
                ts: str = ts_ym,
                out_url: list[str] = fetched_detail_url,
            ) -> bytes | None:
                nonlocal detail_urls
                if detail_urls is None:
                    detail_urls = scrape_pdf_urls(
                        KS_DETAIL_PAGE, DETAIL_PDF_NAME_RE, "detail", report=report, log=logger,
                    )
                target_url = build_download_url(
                    lbl,
                    detail_urls,
                    kind="detail",
                    hashes=DETAIL_PDF_HASHES,
                    filename_prefix="sports-wagering-monthly-detail",
                    log=logger,
                )
                if not target_url:
                    logger(f"INFO {lbl}: no detail PDF discovered; leaving split columns blank")
                    return None
                out_url[:] = [target_url]
                downloaded = fetch_with_wayback_fallback(
                    target_url, timestamp_yyyymm=ts, signature_check=b"%PDF", log=logger,
                )
                if report is not None:
                    report.add_probe(
                        f"{lbl}:detail",
                        target_url,
                        status="ok" if downloaded else "fail",
                        reason="downloaded" if downloaded else "fetch-failed",
                    )
                return downloaded

            detail_data = load_cached_or_fetch(
                cache_file=detail_path,
                fetcher=_fetch_detail,
                log=logger,
                cache_hit_message=f"INFO using cached detail file for {label}",
                fetch_success_message=f"INFO {label}: downloaded detail PDF",
                fetch_error_message=f"could not download Kansas detail PDF for {label}",
                validate=lambda payload: payload.startswith(b"%PDF"),
                cache_ttl_seconds=ttl_seconds,
                offline=offline,
            )
            detail_url = (
                fetched_detail_url[0]
                if fetched_detail_url
                else build_download_url(
                    label,
                    detail_urls or {},
                    kind="detail",
                    hashes=DETAIL_PDF_HASHES,
                    filename_prefix="sports-wagering-monthly-detail",
                    log=lambda _m: None,
                )
            )

            summary_tax: float | None = None
            summary_op_tax: dict[str, float] = {}
            if summary_data_bytes:
                summary_tax, summary_op_tax = parse_summary_fiscal(summary_data_bytes, log=logger)

            state_gross_rev: float | None = None
            state_promo: float | None = None
            state_tax: float | None = summary_tax
            operator_details = []
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
                    detail_state_fiscal, detail_op_fiscal = parse_detail_fiscal(detail_data, label, log=logger)
                    d_gross, d_promo, d_tax = detail_state_fiscal
                    if d_gross is not None:
                        state_gross_rev = d_gross
                    if d_promo is not None:
                        state_promo = d_promo
                    if d_tax is not None:
                        state_tax = d_tax
                    raw_ops = parse_detail_operators(detail_data, label, log=logger)
                    enriched_ops = []
                    for op_tuple in raw_ops:
                        op_key = op_tuple[0]
                        if " - " in op_key:
                            c_part, p_part = op_key.split(" - ", 1)
                            canon_key = _ks_fiscal_key(c_part, p_part)
                        else:
                            canon_key = op_key.lower()
                        op_gross, op_promo, op_tax = detail_op_fiscal.get(canon_key, (None, None, None))
                        if op_tax is None:
                            op_tax = summary_op_tax.get(canon_key)
                        enriched_ops.append((*op_tuple, op_gross, op_promo, op_tax))
                    operator_details = build_operator_results(iso_month, enriched_ops)
                else:
                    logger(f"WARN {label}: detail PDF parse returned no split rows")

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
                reconcile_splits=False,
                details=operator_details,
                gross_revenue=state_gross_rev,
                promo_deductions=state_promo,
                tax=state_tax,
                log=logger,
                label=label,
            )
            if item is None:
                continue
            results.append(item)
            logger(
                f"INFO parsed {label} via {summary_strategy}: handle={item.handle:,.0f} revenue={item.revenue:,.0f} "
                f"retail_handle={item.retail_handle if item.retail_handle is not None else 'n/a'} "
                f"mobile_handle={item.mobile_handle if item.mobile_handle is not None else 'n/a'} hold={item.hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
