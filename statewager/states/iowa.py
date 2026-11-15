"""Iowa sports wagering regulatory fetcher and parser (IRGC)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import io
import os
import re
from typing import Callable

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

from ..cache import cache_path, load_cache, load_cached_or_fetch, save_cache
from ..discover import AnchorParser, DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_text, fetch_with_wayback_fallback
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
from ..parse import extract_pdf_text, parse_dollar

STATE_NAME = "Iowa"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

PUBLICATIONS_URL = "https://irgc.iowa.gov/publications-reports/sports-wagering-revenue"
ARCHIVED_PUBLICATIONS_URL = (
    "https://irgc.iowa.gov/publications-reports/sports-wagering-revenue/archived-sports-revenue"
)
MEDIA_IDS = {
    "Jul 2025": 428,
    "Aug 2025": 436,
    "Sep 2025": 445,
    "Oct 2025": 450,
    "Nov 2025": 455,
    "Dec 2025": 459,
    "Jan 2026": 463,
    "Feb 2026": 468,
    "Mar 2026": 472,
    "Apr 2026": 479,
    "May 2026": 485,
}
FY_MEDIA_IDS = {
    (2023, 2024): 335,
    (2024, 2025): 425,
    (2025, 2026): 508,
}
_DEFAULT_LOG = build_state_logger("IA")
_MEDIA_HREF_RE = re.compile(r"/media/(\d+)/download", re.IGNORECASE)
_COMPACT_LABEL_RE = re.compile(
    r"(SPORTS WAGERING NET RECEIPTS|SPORTS WAGERING HANDLE|RETAIL NET RECEIPTS|RETAIL HANDLE|INTERNET NET RECEIPTS|INTERNET HANDLE)",
    re.IGNORECASE,
)

IowaMetrics = tuple[float, float, float | None, float | None, float | None, float | None]
IowaDetailRow = tuple[str, float, float, float | None, float | None, float | None, float | None]


@dataclass(frozen=True)
class ParsedIowaMetrics:
    metrics: IowaMetrics
    layout: str
    details: tuple[IowaDetailRow, ...] = ()
    tax: float | None = None


TOTALS_LABELS = [
    "SPORTS WAGERING NET RECEIPTS",
    "SPORTS WAGERING HANDLE",
    "SPORTS WAGERING PAYOUTS",
    "RETAIL NET RECEIPTS",
    "RETAIL HANDLE",
    "RETAIL PAYOUTS",
    "INTERNET NET RECEIPTS",
    "INTERNET HANDLE",
    "INTERNET PAYOUTS",
    "STATE TAX",
]
TOTAL_HANDLE_LABEL = "SPORTS WAGERING HANDLE"
TOTAL_REVENUE_LABEL = "SPORTS WAGERING NET RECEIPTS"
RETAIL_HANDLE_LABEL = "RETAIL HANDLE"
RETAIL_REVENUE_LABEL = "RETAIL NET RECEIPTS"
INTERNET_HANDLE_LABEL = "INTERNET HANDLE"
INTERNET_REVENUE_LABEL = "INTERNET NET RECEIPTS"
TOTALS_BOUNDARY_MARKERS = (
    "ONLINE SPORTS WAGERING BY OPERATOR",
    "SPORTS WAGERING REVENUE REPORT -- ",
)
MONEY_PATTERN = r"\(\$[\d,]+(?:\.\d+)?\)|\$[\d,]+(?:\.\d+)?"
MONEY_RE = re.compile(MONEY_PATTERN)

_MEDIA_LINK_RE = re.compile(
    r'href="(/media/(\d+)/download[^"]*)"[^>]*>\s*([^<]+?)\s*<',
    re.IGNORECASE,
)
_LABEL_MONTH_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})",
    re.IGNORECASE,
)
_FY_RANGE_RE = re.compile(r"(20\d{2})\s*[-/]\s*(20\d{2})")
_FY_SINGLE_RE = re.compile(r"(?:Fiscal\s+Year|FY)\s*(20\d{2})\b", re.IGNORECASE)


def media_download_url(media_id: int) -> str:
    return f"https://irgc.iowa.gov/media/{media_id}/download?inline"


def _parse_fy_media_links(links: list[dict[str, str]]) -> dict[tuple[int, int], int]:
    discovered: dict[tuple[int, int], int] = {}
    for link in links:
        href = link.get("href", "")
        id_match = _MEDIA_HREF_RE.search(href)
        if not id_match:
            continue
        label = link.get("text") or link.get("title") or ""
        fy_key: tuple[int, int] | None = None
        range_match = _FY_RANGE_RE.search(label)
        if range_match:
            fy_start, fy_end = int(range_match.group(1)), int(range_match.group(2))
            if fy_end == fy_start + 1:
                fy_key = (fy_start, fy_end)
        if fy_key is None:
            single_match = _FY_SINGLE_RE.search(label)
            if single_match:
                fy_end = int(single_match.group(1))
                fy_key = (fy_end - 1, fy_end)
        if fy_key is not None and fy_key not in discovered:
            discovered[fy_key] = int(id_match.group(1))
    return discovered


def discover_fy_media_ids(
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[tuple[int, int], int]:
    """Discover FY archive media IDs from IRGC archive and publications pages."""
    discovered: dict[tuple[int, int], int] = {}
    for index_url in (ARCHIVED_PUBLICATIONS_URL, PUBLICATIONS_URL):
        links = crawl_anchors(index_url, log=log)
        for fy_key, media_id in _parse_fy_media_links(links).items():
            discovered.setdefault(fy_key, media_id)
    if discovered:
        log(f"INFO discovered {len(discovered)} FY archive media ID(s) on IRGC")
    return discovered


def download_pdf(
    media_id: int,
    label_hint: str | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    url = media_download_url(media_id)
    timestamp = None
    if label_hint:
        try:
            timestamp = datetime.strptime(label_hint, "%b %Y").strftime("%Y%m")
        except ValueError:
            timestamp = None
    data = fetch_with_wayback_fallback(
        url, timestamp_yyyymm=timestamp, signature_check=b"%PDF", log=log,
    )
    if data and data.startswith(b"%PDF"):
        return data
    if data:
        log(f"WARN media/{media_id} did not return PDF content")
    return None


def discover_media_ids(log: Callable[[str], None] = _DEFAULT_LOG) -> dict[str, int]:
    """Scrape the IRGC publications page to find media IDs for monthly reports."""
    found: dict[str, int] = {}
    html: str | None = None
    try:
        html = fetch_text(PUBLICATIONS_URL)
    except Exception as exc:
        log(f"WARN could not fetch IRGC publications page: {exc}")
        return found
    if not html:
        return found
    parser = AnchorParser()
    parser.feed(html)
    for link in parser.links:
        href = link.get("href", "")
        id_match = _MEDIA_HREF_RE.search(href)
        if not id_match:
            continue
        combined = " ".join([link.get("text", ""), link.get("title", ""), href])
        lower_combined = combined.lower()
        if "fiscal" in lower_combined or "amend" in lower_combined:
            continue
        if "sports wagering" not in lower_combined and "sports" not in lower_combined:
            continue
        label = extract_month_label(combined)
        if label:
            found.setdefault(label, int(id_match.group(1)))
    if not found:
        for _href, media_id, text in _MEDIA_LINK_RE.findall(html):
            lower_text = text.lower()
            if "sports wagering" not in lower_text or "fiscal" in lower_text or "amend" in lower_text:
                continue
            match = _LABEL_MONTH_RE.search(text)
            if not match:
                continue
            try:
                label = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%B %Y").strftime("%b %Y")
            except ValueError:
                continue
            found.setdefault(label, int(media_id))
    return found


def get_fy(cal_month: int, cal_year: int) -> tuple[int, int]:
    return fiscal_year_july(cal_year, cal_month)


def parse_text_blob(text: str) -> tuple[float, float] | None:
    revenue_candidates: list[float] = []
    handle_candidates: list[float] = []
    for line in text.splitlines():
        stripped = line.strip()
        if "SPORTS WAGERING NET RECEIPTS" in stripped.upper():
            values = [parse_dollar(token) for token in MONEY_RE.findall(stripped)]
            parsed = [value for value in values if value is not None]
            if parsed:
                revenue_candidates.append(parsed[-1])
        elif "SPORTS WAGERING HANDLE" in stripped.upper():
            values = [parse_dollar(token) for token in MONEY_RE.findall(stripped)]
            parsed = [value for value in values if value is not None]
            if parsed:
                handle_candidates.append(parsed[-1])
    if revenue_candidates and handle_candidates:
        return handle_candidates[-1], revenue_candidates[-1]
    return None


_TOTALS_WORD_RE = re.compile(r"\bTOTALS\b")


def _totals_block_end(text: str, start: int) -> int:
    upper = text.upper()
    end_candidates = [upper.find(marker, start) for marker in TOTALS_BOUNDARY_MARKERS]
    valid = [index for index in end_candidates if index >= 0]
    return min(valid) if valid else len(text)


def _find_statewide_totals_block(text: str) -> str | None:
    upper = text.upper()
    candidates: list[tuple[int, int, int, int, str]] = []
    for match in _TOTALS_WORD_RE.finditer(upper):
        start = match.start()
        line_start = upper.rfind("\n", 0, start) + 1
        line_prefix = text[line_start:start].strip()
        end = _totals_block_end(text, start)
        block = text[start:end]
        block_upper = upper[start:end]
        label_count = sum(1 for label in TOTALS_LABELS if label in block_upper)
        if label_count < 2:
            continue
        header_score = 3 if not line_prefix else 0
        if line_prefix.endswith("-"):
            header_score = 1
        candidates.append((label_count, header_score, start, len(block), block))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    return candidates[0][4]


def _extract_row_total(block: str, label: str) -> float | None:
    upper = block.upper()
    label_pos = upper.find(label)
    if label_pos < 0:
        return None
    next_positions = [
        upper.find(next_label, label_pos + len(label))
        for next_label in TOTALS_LABELS
        if next_label != label
    ]
    next_positions = [pos for pos in next_positions if pos > label_pos]
    segment = block[label_pos: min(next_positions) if next_positions else None]
    values = [parse_dollar(token) for token in MONEY_RE.findall(segment)]
    parsed = [value for value in values if value is not None]
    return parsed[-1] if parsed else None


def _parse_labeled_totals_block(block: str) -> ParsedIowaMetrics | None:
    total_handle = _extract_row_total(block, TOTAL_HANDLE_LABEL)
    total_revenue = _extract_row_total(block, TOTAL_REVENUE_LABEL)
    if total_handle is None or total_revenue is None:
        return None
    return ParsedIowaMetrics(
        metrics=(
            total_handle,
            total_revenue,
            _extract_row_total(block, RETAIL_HANDLE_LABEL),
            _extract_row_total(block, RETAIL_REVENUE_LABEL),
            _extract_row_total(block, INTERNET_HANDLE_LABEL),
            _extract_row_total(block, INTERNET_REVENUE_LABEL),
        ),
        layout="labeled-totals",
        tax=_extract_row_total(block, "STATE TAX"),
    )


def _parse_compact_totals_block(block: str) -> ParsedIowaMetrics | None:
    first_label_match = _COMPACT_LABEL_RE.search(block)
    first_value_match = MONEY_RE.search(block)
    if not first_value_match or (first_label_match and first_label_match.start() < first_value_match.start()):
        return None
    parsed = [
        value
        for token in MONEY_RE.findall(block)
        if (value := parse_dollar(token)) is not None
    ]
    if len(parsed) < 8:
        return None
    tax = parsed[9] if len(parsed) >= 10 and "STATE TAX" in block.upper() else None
    return ParsedIowaMetrics(
        metrics=(parsed[1], parsed[0], parsed[4], parsed[3], parsed[7], parsed[6]),
        layout="compact-totals",
        tax=tax,
    )


def parse_totals_block(text: str) -> ParsedIowaMetrics | None:
    block = _find_statewide_totals_block(text)
    if block is None:
        return None
    parsed = _parse_labeled_totals_block(block)
    if parsed is not None:
        return parsed
    return _parse_compact_totals_block(block)


def parse_month_section(
    text: str,
    label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> ParsedIowaMetrics | None:
    heading_label = datetime.strptime(label, "%b %Y").strftime("%B %Y").upper()
    heading = f"SPORTS WAGERING REVENUE REPORT -- {heading_label}"
    start = text.upper().find(heading)
    if start < 0:
        log(f"WARN Iowa archive section not found for {label}")
        return None
    next_marker = text.upper().find("SPORTS WAGERING REVENUE REPORT -- ", start + len(heading))
    section = text[start: next_marker if next_marker >= 0 else None]
    parsed = parse_totals_block(section)
    if parsed is not None:
        return parsed
    totals = parse_text_blob(section)
    if totals is None:
        return None
    return ParsedIowaMetrics(
        metrics=(totals[0], totals[1], None, None, None, None),
        layout="month-section-text-fallback",
    )


_WORD_MONEY_RE = re.compile(r"^(?:\(\$?[\d,]+\.\d{2}\)|-?\$?[\d,]+\.\d{2})$")
_SUPERSCRIPT_RE = re.compile(r"[¹²³⁴⁵⁶⁷⁸⁹⁰]+")
_DETAIL_METRIC_LABELS: frozenset[str] = frozenset({
    "SPORTS WAGERING NET RECEIPTS",
    "SPORTS WAGERING HANDLE",
    "SPORTS WAGERING PAYOUTS",
    "RETAIL NET RECEIPTS",
    "RETAIL HANDLE",
    "RETAIL PAYOUTS",
    "INTERNET NET RECEIPTS",
    "INTERNET HANDLE",
    "INTERNET PAYOUTS",
    "STATE TAX",
    "NET RECEIPTS",
    "HANDLE",
    "PAYOUTS",
})
_REV_HEADER_RE = re.compile(r"SPORTS WAGERING REVENUE REPORT -- ([A-Z0-9 -]+)")
_ONL_HEADER_RE = re.compile(r"ONLINE SPORTS WAGERING BY OPERATOR -- ([A-Z0-9 -]+)")


def _group_page_word_lines(page) -> list[list[dict]]:
    words = page.extract_words(keep_blank_chars=False, x_tolerance=1.5, y_tolerance=2)
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda item: (round(item["top"] / 3.0), item["x0"])):
        if not lines or abs(word["top"] - lines[-1][0]["top"]) > 3.5:
            lines.append([word])
        else:
            lines[-1].append(word)
    for line in lines:
        line.sort(key=lambda item: item["x0"])
    return lines


def parse_iowa_pdf_details(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[str, list[IowaDetailRow]]:
    """Extract per-casino retail and per-operator online details keyed by ``'Mon YYYY'``."""
    if pdfplumber is None:
        return {}
    by_label: dict[str, list[IowaDetailRow]] = {}
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            kind: str | None = None
            month_banner: str | None = None
            for page in pdf.pages:
                text_upper = (page.extract_text() or "").upper()
                rev_match = _REV_HEADER_RE.search(text_upper)
                onl_match = _ONL_HEADER_RE.search(text_upper)
                if rev_match or onl_match:
                    kind = "rev" if rev_match else "online"
                    month_banner = (rev_match or onl_match).group(1).splitlines()[0].strip()
                elif "ADVANCED DEPOSIT" in text_upper or "FANTASY SPORTS" in text_upper:
                    kind = None
                    month_banner = None
                    continue
                if not kind or not month_banner or any(tok in month_banner for tok in ("FISCAL", "THROUGH", "FYTD")):
                    continue
                try:
                    label = datetime.strptime(month_banner.title(), "%B %Y").strftime("%b %Y")
                except ValueError:
                    continue

                lines = _group_page_word_lines(page)
                blocks: list[tuple[list[list[dict]], dict[str, list[dict]]]] = []
                header_lines: list[list[dict]] = []
                metric_rows: dict[str, list[dict]] = {}
                for line in lines:
                    full_upper = " ".join(w["text"] for w in line).upper().strip()
                    if "SPORTS WAGERING REVENUE REPORT" in full_upper or "ONLINE SPORTS WAGERING BY OPERATOR" in full_upper:
                        continue
                    money_words = [w for w in line if _WORD_MONEY_RE.match(w["text"])]
                    if money_words:
                        first_money_x = money_words[0]["x0"]
                        left_txt = " ".join(w["text"] for w in line if w["x0"] < first_money_x - 2).upper().strip()
                        if left_txt in _DETAIL_METRIC_LABELS:
                            metric_rows[left_txt] = money_words
                            continue
                    if metric_rows:
                        blocks.append((header_lines, metric_rows))
                        header_lines = []
                        metric_rows = {}
                    if line[0]["x0"] < 100 and full_upper != "TOTALS":
                        continue
                    if not money_words:
                        header_lines.append(line)
                if metric_rows:
                    blocks.append((header_lines, metric_rows))
                if not blocks:
                    continue

                entries = by_label.setdefault(label, [])
                for hdrs, mrows in blocks:
                    ref_key = "RETAIL HANDLE" if kind == "rev" else (
                        "INTERNET HANDLE" if "INTERNET HANDLE" in mrows else "HANDLE"
                    )
                    rev_key = "RETAIL NET RECEIPTS" if kind == "rev" else (
                        "INTERNET NET RECEIPTS" if "INTERNET NET RECEIPTS" in mrows else "NET RECEIPTS"
                    )
                    if ref_key not in mrows or rev_key not in mrows:
                        continue
                    cols_h = mrows[ref_key]
                    cols_r = mrows[rev_key]
                    ncols = len(cols_h)
                    equal_mrows = [mw for mw in mrows.values() if len(mw) == ncols] or [cols_h]
                    col_x1 = [max(mw[c]["x1"] for mw in equal_mrows) for c in range(ncols)]
                    bounds = [col_x1[c] + 6.0 for c in range(ncols - 1)]
                    names: list[list[str]] = [[] for _ in range(ncols)]
                    for hline in hdrs:
                        for word in hline:
                            mid = (word["x0"] + word["x1"]) / 2.0
                            col_idx = 0
                            while col_idx < len(bounds) and mid > bounds[col_idx]:
                                col_idx += 1
                            names[col_idx].append(word["text"])
                    for col_idx in range(ncols):
                        raw_name = _SUPERSCRIPT_RE.sub("", " ".join(names[col_idx])).strip()
                        if not raw_name or raw_name.upper() == "TOTALS":
                            continue
                        hv = parse_dollar(cols_h[col_idx]["text"])
                        rw = min(cols_r, key=lambda w: abs(w["x1"] - col_x1[col_idx]))
                        rv = parse_dollar(rw["text"])
                        if hv is None or rv is None or (hv == 0.0 and rv == 0.0):
                            continue
                        if kind == "rev":
                            entries.append((f"{raw_name} (Retail)", hv, rv, hv, rv, None, None))
                        else:
                            entries.append((raw_name, hv, rv, None, None, hv, rv))
    except Exception as exc:
        log(f"WARN could not extract Iowa detail breakdown: {exc}")
    return by_label


def parse_iowa_pdf(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    label_hint: str | None = None,
) -> ParsedIowaMetrics | None:
    text = extract_pdf_text(data, log=log)
    if not text:
        return None
    detail_map = parse_iowa_pdf_details(data, log=log)
    matched_details: tuple[IowaDetailRow, ...] = ()
    if label_hint and label_hint in detail_map:
        matched_details = tuple(detail_map[label_hint])
    elif len(detail_map) == 1:
        matched_details = tuple(next(iter(detail_map.values())))
    parsed = parse_totals_block(text)
    if parsed is not None:
        return ParsedIowaMetrics(
            metrics=parsed.metrics,
            layout=parsed.layout,
            details=matched_details,
            tax=parsed.tax,
        )
    totals = parse_text_blob(text)
    if totals is None:
        return None
    return ParsedIowaMetrics(
        metrics=(totals[0], totals[1], None, None, None, None),
        layout="text-fallback",
        details=matched_details,
    )


def parse_local_file(
    local_dir: str,
    label: str,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> ParsedIowaMetrics | None:
    normalized = label.replace(" ", "").lower()
    for entry in sorted(os.listdir(local_dir)):
        if not entry.lower().endswith(".pdf"):
            continue
        if normalized in entry.replace(" ", "").lower():
            with open(os.path.join(local_dir, entry), "rb") as file_handle:
                return parse_iowa_pdf(file_handle.read(), log=log, label_hint=label)
    return None


def _load_cached_monthly_pdf(
    year: int,
    month: int,
    label: str,
    cache_dir: str | None,
    *,
    migrate_legacy: bool = True,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    canonical = cache_path(cache_dir, f"iowa_splits_{year}_{month:02d}.pdf")
    data = load_cache(canonical)
    if data:
        log(f"INFO using cached monthly PDF for {label}")
        return data
    legacy = cache_path(cache_dir, f"iowa_{year}_{month:02d}.pdf")
    data = load_cache(legacy)
    if data:
        log(f"INFO using legacy cached monthly PDF for {label}")
        if migrate_legacy and canonical and canonical != legacy:
            try:
                save_cache(canonical, data)
            except OSError as exc:
                log(f"WARN could not migrate cache {legacy} -> {canonical}: {exc}")
        return data
    return None


def _fetch_monthly_pdf_with_lookup(
    year: int,
    month: int,
    label: str,
    media_lookup: dict[str, int],
    cache_dir: str | None,
    *,
    offline: bool = False,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    cached = _load_cached_monthly_pdf(
        year, month, label, cache_dir, migrate_legacy=not offline, log=log,
    )
    if cached is not None:
        return cached
    if offline:
        return None
    media_id = media_lookup.get(label)
    if media_id is None:
        log(f"WARN no IRGC media ID known for {label}")
        return None
    data = download_pdf(media_id, label_hint=label, log=log)
    if data is None:
        log(f"WARN could not download Iowa monthly PDF for {label}")
        return None
    canonical = cache_path(cache_dir, f"iowa_splits_{year}_{month:02d}.pdf")
    try:
        save_cache(canonical, data)
    except OSError as exc:
        log(f"WARN could not write cache {canonical}: {exc}")
    return data


def build_archive_lookup(
    months: list[tuple[int, int, str]],
    cache_dir: str | None,
    *,
    offline: bool = False,
    log: Callable[[str], None] = _DEFAULT_LOG,
    fy_lookup: dict[tuple[int, int], int] | None = None,
) -> dict[str, ParsedIowaMetrics]:
    fy_map = fy_lookup if fy_lookup is not None else FY_MEDIA_IDS
    archive_lookup: dict[str, ParsedIowaMetrics] = {}
    needed_fys = sorted({get_fy(month, year) for month, year, _ in months if get_fy(month, year) in fy_map})
    for fy in needed_fys:
        try:
            media_id = fy_map[fy]
            archive_path = cache_path(cache_dir, f"iowa_fy_{fy[0]}_{fy[1]}_archive.pdf")
            data = load_cached_or_fetch(
                cache_file=archive_path,
                fetcher=lambda mid=media_id: download_pdf(mid, log=log),
                log=log,
                cache_hit_message=f"INFO using cached FY archive PDF for {fy[0]}-{fy[1]}",
                fetch_error_message=f"could not download Iowa FY archive PDF media/{media_id}",
                offline=offline,
            )
            if data is None:
                log(f"WARN could not load FY archive PDF media/{media_id}")
                continue
            text = extract_pdf_text(data, log=log)
            if not text:
                log(f"WARN could not extract text from Iowa FY archive {fy[0]}-{fy[1]}")
                continue
            detail_lookup = parse_iowa_pdf_details(data, log=log)
            for month, year, label in months:
                if get_fy(month, year) != fy or label in archive_lookup:
                    continue
                parsed = parse_month_section(text, label, log=log)
                if parsed is not None:
                    archive_lookup[label] = ParsedIowaMetrics(
                        metrics=parsed.metrics,
                        layout=parsed.layout,
                        details=tuple(detail_lookup.get(label, ())),
                        tax=parsed.tax,
                    )
                else:
                    log(f"WARN Iowa FY archive did not yield totals for {label}")
        except Exception as exc:
            log(f"WARN failed Iowa FY archive processing for {fy[0]}-{fy[1]}: {exc}")
    return archive_lookup


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
    """Fetch and parse Iowa monthly sports wagering results."""
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    months = [(mdate.month, mdate.year, mdate.strftime("%b %Y")) for mdate in target_dates]
    fy_lookup = dict(FY_MEDIA_IDS)
    if not offline:
        requested_fys = {get_fy(month, year) for month, year, _ in months}
        if requested_fys - fy_lookup.keys():
            fy_lookup.update(discover_fy_media_ids(log=logger))
    archive_lookup = build_archive_lookup(
        months, cache_dir, offline=offline, log=logger, fy_lookup=fy_lookup,
    )
    media_lookup: dict[str, int] = dict(MEDIA_IDS)
    requested_labels = {label for _, _, label in months}
    missing_labels = requested_labels - media_lookup.keys()
    if missing_labels and not offline:
        discovered = discover_media_ids(log=logger)
        for disc_label, media_id in discovered.items():
            media_lookup.setdefault(disc_label, media_id)
        newly_added = sorted(set(media_lookup) - set(MEDIA_IDS))
        if newly_added:
            logger(f"INFO discovered IRGC media IDs for: {newly_added}")
    if report is not None:
        report.add_index(PUBLICATIONS_URL, anchor_count=len(media_lookup))
        for rpt_label, media_id in media_lookup.items():
            if rpt_label in requested_labels:
                report.add_anchor(f"{rpt_label}:summary", f"media/{media_id}")

    results: list[StateMonthResult] = []
    for month, year, label in months:
        iso_month = format_iso_month(year, month)
        try:
            logger(f"INFO processing {label}")
            parsed_result: ParsedIowaMetrics | None = None
            source_url: str | None = None
            if local_dir:
                parsed_result = parse_local_file(local_dir, label, log=logger)
                if parsed_result:
                    logger(f"INFO parsed local PDF for {label} ({parsed_result.layout})")
                    source_url = PUBLICATIONS_URL
            if parsed_result is None and label in archive_lookup:
                parsed_result = archive_lookup[label]
                logger(f"INFO parsed FY archive for {label} ({parsed_result.layout})")
                fy_key = get_fy(month, year)
                source_url = media_download_url(fy_lookup[fy_key]) if fy_key in fy_lookup else PUBLICATIONS_URL
            if parsed_result is None:
                data = _fetch_monthly_pdf_with_lookup(
                    year, month, label, media_lookup, cache_dir, offline=offline, log=logger,
                )
                if data:
                    parsed_result = parse_iowa_pdf(data, log=logger, label_hint=label)
                    if parsed_result is None:
                        logger(f"WARN {label}: monthly PDF did not yield totals")
                    else:
                        logger(f"INFO parsed monthly PDF for {label} ({parsed_result.layout})")
                        source_url = (
                            media_download_url(media_lookup[label])
                            if label in media_lookup
                            else PUBLICATIONS_URL
                        )
            if parsed_result is None:
                log_skip(logger, label, "no matching local PDF or known media ID")
                continue
            total_handle, total_revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue = parsed_result.metrics
            operator_details = build_operator_results(iso_month, parsed_result.details)
            item = finalize_state_month(
                state=STATE_NAME,
                iso_month=iso_month,
                handle=total_handle,
                revenue=total_revenue,
                retail_handle=retail_handle,
                retail_revenue=retail_revenue,
                mobile_handle=mobile_handle,
                mobile_revenue=mobile_revenue,
                source_url=source_url,
                details=operator_details,
                strict_split_pct=0.01,
                gross_revenue=total_revenue,
                tax=parsed_result.tax,
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
                f"hold={item.hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
