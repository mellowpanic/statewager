"""Kansas sports wagering regulatory fetcher and parser (Kansas Lottery)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import re
import shutil
from typing import Callable
from urllib.parse import urljoin

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from ..cache import cache_path, find_local_month_file, load_cached_or_fetch, recent_cache_ttl
from ..discover import DiscoveryReport, crawl_anchors, extract_month_label, parse_text_month
from ..http import fetch_with_wayback_fallback
from ..model import (
    MONTH_ABBR,
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_month_result,
    log_skip,
    parse_month_spec,
    resolve_fetch_months,
)
from ..parse import (
    PDF_MAGIC,
    PDFPLUMBER_LOCK,
    clean_lower,
    collapse_ws,
    extract_pdf_text,
    extract_pdf_text_with_ocr,
    is_pdf_bytes,
    parse_dollar,
)

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
_PROVIDER_SPLIT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9'/.\- ]*?)\s+(?=[+\-\u2212]?\(?\$?\d|-(?:\s|$))")
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
                report.add_unmatched_link(link, "not-pdf", href=href)
            continue
        match = pdf_name_re.search(href)
        if match:
            year, month_num = int(match.group(1)), int(match.group(2))
            if not (1 <= month_num <= 12):
                continue
            label = f"{MONTH_ABBR[month_num]} {year}"
        else:
            meta = " ".join(unescape(link.get(key, "")) for key in ("title", "text"))
            label = parse_text_month(meta) or extract_month_label(" ".join([href, meta]))
            if not label:
                if report is not None:
                    report.add_unmatched_link(link, "no-month-token", href=href)
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
    month = parse_month_spec(label)
    log(f"INFO using hardcoded {kind} hash for {label}")
    return f"{KS_LOTTERY_BASE}/media/{hash_value}/{filename_prefix}-{month.year}-{month.month:02d}.pdf"


normalize_pdf_label = clean_lower


def _extract_handle_and_revenue_from_line(line: str) -> tuple[float | None, float | None]:
    tokens = AMOUNT_TOKEN_RE.findall(line)
    if "subtotal -" in normalize_pdf_label(line) and tokens and tokens[0] == "-":
        tokens = tokens[1:]
    rev_idx = 5 if len(tokens) >= 6 else (1 if len(tokens) >= 2 else None)
    if rev_idx is None:
        return None, None
    handle = parse_dollar(tokens[0])
    if handle is None:
        return None, None
    revenue = 0.0 if tokens[rev_idx] == "-" else parse_dollar(tokens[rev_idx])
    return handle, revenue


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


class _KsPdfPageCache:
    """Lazy per-PDF cache for page-1 pdftotext -raw and pdfplumber tables/text."""

    def __init__(self, data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> None:
        self.data = data
        self.log = log
        self._has_raw = False
        self._raw_text: str | None = None
        self._has_label = False
        self._label_hint: str | None = None
        self._has_pdf = False
        self._has_pages = False
        self._tables: list[list[list[str | None]]] = []
        self._page_text = ""
        self._flat_rows: list[list[object]] | None = None

    def raw_text(self) -> str | None:
        if not self._has_raw:
            self._raw_text = _extract_pdftotext_raw(self.data)
            self._has_raw = True
        return self._raw_text

    def label_hint(self) -> str | None:
        if not self._has_label:
            self._label_hint = parse_text_month(self.raw_text() or "")
            self._has_label = True
        return self._label_hint

    def pdf_page0(self) -> tuple[bool, list[list[list[str | None]]], str]:
        if not self._has_pdf:
            self._has_pdf = True
            if pdfplumber is not None:
                try:
                    with PDFPLUMBER_LOCK, pdfplumber.open(io.BytesIO(self.data)) as pdf:
                        if pdf.pages:
                            self._has_pages = True
                            page = pdf.pages[0]
                            self._tables = page.extract_tables() or []
                            self._page_text = page.extract_text() or ""
                except Exception as exc:
                    self.log(f"WARN could not parse Kansas PDF with pdfplumber: {exc}")
        return self._has_pages, self._tables, self._page_text

    def flattened_rows(self) -> tuple[bool, list[list[object]]]:
        has_pages, tables, _text = self.pdf_page0()
        if not has_pages:
            return False, []
        if self._flat_rows is None:
            self._flat_rows = [r for t in tables for r in t if any(c and str(c).strip() for c in r)]
        return True, self._flat_rows


def _count_usable_words(text: str) -> int:
    return len(_USABLE_WORD_RE.findall(text or ""))


def parse_summary_pdf(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    _page_cache: _KsPdfPageCache | None = None,
) -> tuple[tuple[float, float], str] | None:
    cache = _page_cache or _KsPdfPageCache(data, log)
    fallback_text = cache.raw_text()
    if fallback_text:
        extracted = _extract_from_text(fallback_text)
        if extracted is not None:
            return (extracted[0], extracted[1]), "pdftotext_raw"
    if pdfplumber is not None:
        has_pages, tables, text = cache.pdf_page0()
        if not has_pages:
            return None
        total = None
        for table in tables:
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
        if text and not _looks_fragmented_text(text):
            extracted = _extract_from_text(text)
            if extracted is not None:
                return (extracted[0], extracted[1]), "pdfplumber_text"
    log("WARN Kansas PDF parse returned no totals from table or text")
    return None


def parse_detail_pdf(
    data: bytes,
    month_label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    _page_cache: _KsPdfPageCache | None = None,
) -> tuple[tuple[float, float, float | None, float | None, float | None, float | None], str] | None:
    """Parse Kansas detail splits."""
    cache = _page_cache or _KsPdfPageCache(data, log)
    fallback_text = cache.raw_text()
    label = cache.label_hint() or month_label
    usable_word_counts: list[int] = []
    if fallback_text:
        usable_word_counts.append(_count_usable_words(fallback_text))
        extracted = _extract_from_text(fallback_text)
        if extracted is not None:
            return extracted, "pdftotext_raw"
    if pdfplumber is not None:
        has_pages, _tables, text = cache.pdf_page0()
        if not has_pages:
            return None
        usable_word_counts.append(_count_usable_words(text))
        if not _looks_fragmented_text(text):
            extracted = _extract_from_text(text)
            if extracted is not None:
                return extracted, "pdfplumber_text"
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
    parsed = parse_dollar(value)
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
    *,
    _page_cache: _KsPdfPageCache | None = None,
) -> list[tuple[str, float, float, float | None, float | None, float | None, float | None]]:
    """Extract per-(casino, provider) retail + online wagers and net revenues from Kansas detail PDFs."""
    cache = _page_cache or _KsPdfPageCache(data, log)
    fallback_text = cache.raw_text()
    label = cache.label_hint() or month_label
    if label in DETAIL_OPERATOR_OVERRIDES:
        return list(DETAIL_OPERATOR_OVERRIDES[label])

    merged: dict[str, list[float | None]] = {}
    order: list[str] = []

    if pdfplumber is not None:
        has_pages, rows = cache.flattened_rows()
        if has_pages and len(rows) >= 10:
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
                casino = collapse_ws(row[0])
                provider = collapse_ws(row[1] if len(row) > 1 else "")
                if not casino or not provider:
                    continue
                handle = _parse_ks_cell_amount(row[2] if len(row) > 2 else None)
                revenue = _parse_ks_cell_amount(row[7] if len(row) > 7 else None)
                _record_ks_operator_row(merged, order, casino, provider, section, handle, revenue)

    if not merged and fallback_text:
        section = "retail"
        seen_in_retail: set[str] = set()
        for raw_line in fallback_text.splitlines():
            low, casino, provider, tokens = _parse_ks_operator_text_line(raw_line)
            if "subtotal - retail" in low:
                section = "online"
                continue
            if "subtotal - online" in low or low.startswith("totals"):
                break
            if not casino or not provider or len(tokens) < 6:
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
        total_h = (rh or 0.0) + (mh or 0.0)
        total_r = (rr or 0.0) + (mr or 0.0)
        if total_h == 0.0 and total_r == 0.0:
            continue
        output.append((key, total_h, total_r, rh, rr, mh, mr))
    return output


def _ks_fiscal_key(casino: str, provider: str | None = None) -> str:
    if provider is None:
        if " - " not in casino:
            return clean_lower(casino)
        casino, provider = casino.split(" - ", 1)
    clean_c = clean_lower(casino)
    clean_p = _BET365_SPACE_RE.sub("BET365", collapse_ws(provider)).lower()
    return f"{clean_c} - {clean_p}"


def _ks_fiscal_values(
    raw_wagers: object,
    raw_prizes: object,
    raw_promo: object,
    raw_tax: object,
) -> tuple[float, float, float]:
    wagers = _parse_ks_cell_amount(raw_wagers)
    prizes = _parse_ks_cell_amount(raw_prizes)
    return wagers - abs(prizes), abs(_parse_ks_cell_amount(raw_promo)), _parse_ks_cell_amount(raw_tax)


def parse_summary_fiscal(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    _page_cache: _KsPdfPageCache | None = None,
) -> tuple[float | None, dict[str, float]]:
    """Extract Monthly Summary ``(statewide_tax, {canonical_op_key: tax})`` from Kansas summary PDFs."""
    cache = _page_cache or _KsPdfPageCache(data, log)
    state_tax: float | None = None
    op_taxes: dict[str, float] = {}

    if pdfplumber is not None:
        has_pages, tables, _text = cache.pdf_page0()
        if has_pages and tables:
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
                casino = collapse_ws(row[0])
                provider = collapse_ws(row[1] if len(row) > 1 else "")
                if casino and provider:
                    k = _ks_fiscal_key(casino, provider)
                    op_taxes[k] = op_taxes.get(k, 0.0) + tax_val

    if state_tax is None:
        fallback_text = cache.raw_text()
        if fallback_text:
            op_taxes.clear()
            for raw_line in fallback_text.splitlines():
                low, casino, provider, tokens = _parse_ks_operator_text_line(raw_line)
                if "fiscal year" in low:
                    break
                if low.startswith("totals"):
                    if len(tokens) >= 3:
                        state_tax = _parse_ks_cell_amount(tokens[2])
                    break
                if "subtotal -" in low:
                    continue
                if casino and provider and len(tokens) >= 3:
                    k = _ks_fiscal_key(casino, provider)
                    op_taxes[k] = op_taxes.get(k, 0.0) + _parse_ks_cell_amount(tokens[2])
    return state_tax, op_taxes


def _parse_ks_operator_text_line(raw_line: str) -> tuple[str, str | None, str | None, list[str]]:
    """Return ``(collapsed_lower, casino, provider, amount_tokens)`` for a raw pdftotext line."""
    collapsed = _BET365_SPACE_RE.sub("BET365", collapse_ws(raw_line))
    low = collapsed.lower()
    casino = next((prefix for prefix in _CASINO_PREFIXES if collapsed.startswith(prefix + " ")), None)
    if not casino:
        return low, None, None, AMOUNT_TOKEN_RE.findall(collapsed)
    rest = collapsed[len(casino):].strip()
    prov_match = _PROVIDER_SPLIT_RE.match(rest)
    if not prov_match:
        return low, casino, None, AMOUNT_TOKEN_RE.findall(collapsed)
    provider = prov_match.group(1).strip()
    tokens = AMOUNT_TOKEN_RE.findall(rest[prov_match.end():])
    return low, casino, provider, tokens


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
    *,
    _page_cache: _KsPdfPageCache | None = None,
) -> tuple[
    tuple[float | None, float | None, float | None],
    dict[str, tuple[float | None, float | None, float | None]],
]:
    """Extract ``((gross_rev, promo, tax), {canonical_op_key: (gross_rev, promo, tax)})`` from Kansas detail PDFs."""
    del month_label
    cache = _page_cache or _KsPdfPageCache(data, log)
    state_fiscal: tuple[float | None, float | None, float | None] = (None, None, None)
    op_buckets: dict[str, list[float]] = {}

    if pdfplumber is not None:
        has_pages, rows = cache.flattened_rows()
        if has_pages:
            w_col, p_col, pr_col, tx_col = 2, 3, 4, 9
            for row in rows:
                if len(row) >= 7 and "casino" in normalize_pdf_label(row[0] or ""):
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
                gross_rev, promo, tax = _ks_fiscal_values(row[w_col], row[p_col], row[pr_col], row[tx_col])
                if c0 == "totals":
                    state_fiscal = (gross_rev, promo, tax)
                    break
                casino = collapse_ws(row[0])
                provider = collapse_ws(row[1])
                if casino and provider:
                    _add_ks_fiscal(op_buckets, _ks_fiscal_key(casino, provider), gross_rev, promo, tax)

    if state_fiscal == (None, None, None):
        fallback_text = cache.raw_text()
        if fallback_text:
            op_buckets.clear()
            for raw_line in fallback_text.splitlines():
                low, casino, provider, tokens = _parse_ks_operator_text_line(raw_line)
                if "subtotal -" in low or len(tokens) < 7:
                    continue
                gross_rev, promo, tax = _ks_fiscal_values(tokens[0], tokens[1], tokens[2], tokens[-1])
                if low.startswith("totals"):
                    state_fiscal = (gross_rev, promo, tax)
                    break
                if casino and provider:
                    _add_ks_fiscal(op_buckets, _ks_fiscal_key(casino, provider), gross_rev, promo, tax)

    op_fiscal = {k: (v[0], v[1], v[2]) for k, v in op_buckets.items()}
    return state_fiscal, op_fiscal


def _find_local_summary_bytes(local_dir: str, month: int, year: int) -> bytes | None:
    matched = find_local_month_file(local_dir, year, month, ".pdf")
    return matched[1] if matched is not None else None


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
    live_urls: dict[str, dict[str, str] | None] = {"summary": None, "detail": None}
    results: list[StateMonthResult] = []

    def _load_ks_pdf(
        *,
        kind: str,
        page_url: str,
        name_re: re.Pattern[str],
        hashes: dict[str, str],
        filename_prefix: str,
        cache_name: str,
        label: str,
        ts_ym: str,
        ttl_seconds: float | None,
    ) -> tuple[bytes | None, str | None]:
        fetched_url: list[str] = []

        def _resolve_url(url_log: Callable[[str], None]) -> str | None:
            return build_download_url(
                label,
                live_urls[kind] or {},
                kind=kind,
                hashes=hashes,
                filename_prefix=filename_prefix,
                log=url_log,
            )

        def _fetcher() -> bytes | None:
            if live_urls[kind] is None:
                live_urls[kind] = scrape_pdf_urls(page_url, name_re, kind, report=report, log=logger)
            target_url = _resolve_url(logger)
            if not target_url:
                if kind == "summary":
                    logger(f"WARN {label}: no discovered or fallback PDF URL")
                    if report is not None:
                        report.add_probe(f"{label}:summary", "(none)", reason="no-url")
                else:
                    logger(f"INFO {label}: no detail PDF discovered; leaving split columns blank")
                return None
            fetched_url[:] = [target_url]
            downloaded = fetch_with_wayback_fallback(target_url, timestamp_yyyymm=ts_ym, signature_check=PDF_MAGIC, log=logger)
            if report is not None:
                report.add_probe(
                    f"{label}:{kind}",
                    target_url,
                    status="ok" if downloaded else "fail",
                    reason="downloaded" if downloaded else "fetch-failed",
                )
            return downloaded

        tag = "file" if kind == "summary" else "detail file"
        data = load_cached_or_fetch(
            cache_file=cache_path(cache_dir, cache_name),
            fetcher=_fetcher,
            log=logger,
            cache_hit_message=f"INFO using cached {tag} for {label}",
            fetch_success_message=f"INFO {label}: downloaded {kind} PDF",
            fetch_error_message=f"could not download Kansas {kind} PDF for {label}",
            validate=is_pdf_bytes,
            cache_ttl_seconds=ttl_seconds,
            offline=offline,
        )
        return data, (fetched_url[0] if fetched_url else _resolve_url(lambda _m: None))

    for mdate in target_dates:
        month = mdate.month
        year = mdate.year
        label = f"{MONTH_ABBR[month]} {year}"
        iso_month = format_iso_month(year, month)
        ts_ym = f"{year}{month:02d}"
        ttl_seconds = recent_cache_ttl(year, month)
        try:
            logger(f"INFO processing {label}")
            summary_parsed = None
            summary_strategy = None
            retail_handle = retail_revenue = mobile_handle = mobile_revenue = None
            chosen_url: str | None = None
            summary_data_bytes: bytes | None = None
            summary_cache: _KsPdfPageCache | None = None

            if local_dir:
                summary_data_bytes = _find_local_summary_bytes(local_dir, month, year)
                if summary_data_bytes is not None:
                    summary_cache = _KsPdfPageCache(summary_data_bytes, logger)
                    summary_parsed = parse_summary_pdf(summary_data_bytes, log=logger, _page_cache=summary_cache)
                    if summary_parsed:
                        logger(f"INFO {label}: using local file")
                        chosen_url = KS_SUMMARY_PAGE

            if summary_parsed is None:
                summary_data, url = _load_ks_pdf(
                    kind="summary",
                    page_url=KS_SUMMARY_PAGE,
                    name_re=SUMMARY_PDF_NAME_RE,
                    hashes=SUMMARY_PDF_HASHES,
                    filename_prefix="sports-wagering-monthly-revenue",
                    cache_name=f"kansas_{year}_{month:02d}.pdf",
                    label=label,
                    ts_ym=ts_ym,
                    ttl_seconds=ttl_seconds,
                )
                if summary_data:
                    summary_data_bytes = summary_data
                    summary_cache = _KsPdfPageCache(summary_data, logger)
                    summary_parsed = parse_summary_pdf(summary_data, log=logger, _page_cache=summary_cache)
                    chosen_url = url
                    if summary_parsed is None:
                        logger(f"WARN {label}: PDF parse returned no totals")

            if summary_parsed is None:
                log_skip(logger, label, "no matching PDF or parseable download")
                continue

            (handle, revenue), summary_strategy = summary_parsed
            detail_data, detail_url = _load_ks_pdf(
                kind="detail",
                page_url=KS_DETAIL_PAGE,
                name_re=DETAIL_PDF_NAME_RE,
                hashes=DETAIL_PDF_HASHES,
                filename_prefix="sports-wagering-monthly-detail",
                cache_name=f"kansas_detail_{year}_{month:02d}.pdf",
                label=label,
                ts_ym=ts_ym,
                ttl_seconds=ttl_seconds,
            )

            summary_tax, summary_op_tax = parse_summary_fiscal(
                summary_data_bytes, log=logger, _page_cache=summary_cache,
            )
            state_gross_rev: float | None = None
            state_promo: float | None = None
            state_tax: float | None = summary_tax
            operator_details = []
            if detail_data:
                detail_cache = _KsPdfPageCache(detail_data, logger)
                detail_parsed = parse_detail_pdf(detail_data, label, log=logger, _page_cache=detail_cache)
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
                    detail_state_fiscal, detail_op_fiscal = parse_detail_fiscal(
                        detail_data, label, log=logger, _page_cache=detail_cache,
                    )
                    d_gross, d_promo, d_tax = detail_state_fiscal
                    if d_gross is not None:
                        state_gross_rev = d_gross
                    if d_promo is not None:
                        state_promo = d_promo
                    if d_tax is not None:
                        state_tax = d_tax
                    raw_ops = parse_detail_operators(detail_data, label, log=logger, _page_cache=detail_cache)
                    enriched_ops = []
                    for op_tuple in raw_ops:
                        canon_key = _ks_fiscal_key(op_tuple[0])
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
            log_month_result(logger, label, item, prefix=f"parsed {label} via {summary_strategy}")
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
