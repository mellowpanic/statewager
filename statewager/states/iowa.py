"""Iowa sports wagering regulatory fetcher and parser (IRGC)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import os
import re
from typing import Callable

from ..cache import cache_path, load_cached_or_fetch
from ..discover import DiscoveryReport, crawl_anchors, extract_month_label
from ..http import fetch_bytes, fetch_with_wayback_fallback
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

STATE_NAME = "Iowa"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = False

USER_AGENT = "Mozilla/5.0"
PUBLICATIONS_URL = "https://irgc.iowa.gov/publications-reports/sports-wagering-revenue"
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
}
_DEFAULT_LOG = build_state_logger("IA")

IowaMetrics = tuple[float, float, float | None, float | None, float | None, float | None]


@dataclass(frozen=True)
class ParsedIowaMetrics:
    metrics: IowaMetrics
    layout: str


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


def media_download_url(media_id: int) -> str:
    return f"https://irgc.iowa.gov/media/{media_id}/download?inline"


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
    try:
        html_bytes = fetch_bytes(PUBLICATIONS_URL)
    except Exception as exc:
        log(f"WARN could not fetch IRGC publications page: {exc}")
        html_bytes = None
    if html_bytes:
        html = html_bytes.decode("utf-8", errors="replace")
        for _href, media_id, text in _MEDIA_LINK_RE.findall(html):
            if "sports wagering" not in text.lower() or "fiscal" in text.lower() or "amend" in text.lower():
                continue
            match = _LABEL_MONTH_RE.search(text)
            if not match:
                continue
            try:
                label = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%B %Y").strftime("%b %Y")
            except ValueError:
                continue
            found.setdefault(label, int(media_id))
    anchors = crawl_anchors(PUBLICATIONS_URL, log=log)
    media_id_from_href = re.compile(r"/media/(\d+)/download")
    for link in anchors:
        href = link.get("href", "")
        id_match = media_id_from_href.search(href)
        if not id_match:
            continue
        combined = " ".join([link.get("text", ""), link.get("title", ""), href])
        if "fiscal" in combined.lower() or "amend" in combined.lower():
            continue
        if "sports wagering" not in combined.lower() and "sports" not in combined.lower():
            continue
        label = extract_month_label(combined)
        if label:
            found.setdefault(label, int(id_match.group(1)))
    return found


def get_fy(cal_month: int, cal_year: int) -> tuple[int, int]:
    return (cal_year, cal_year + 1) if cal_month >= 7 else (cal_year - 1, cal_year)


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


def _totals_block_end(text: str, start: int) -> int:
    upper = text.upper()
    end_candidates = [upper.find(marker, start) for marker in TOTALS_BOUNDARY_MARKERS]
    valid = [index for index in end_candidates if index >= 0]
    return min(valid) if valid else len(text)


def _find_statewide_totals_block(text: str) -> str | None:
    upper = text.upper()
    candidates: list[tuple[int, int, int, int, str]] = []
    for match in re.finditer(r"\bTOTALS\b", upper):
        start = match.start()
        line_start = upper.rfind("\n", 0, start) + 1
        line_prefix = text[line_start:start].strip()
        block = text[start:_totals_block_end(text, start)]
        label_count = sum(1 for label in TOTALS_LABELS if label in block.upper())
        if label_count < 2:
            continue
        header_score = 3 if not line_prefix else 0
        if line_prefix.endswith("-"):
            header_score = 1
        candidates.append((label_count, header_score, start, len(block), block))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
    best = candidates[0]
    if best[0] < 2:
        return None
    return best[4]


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
    )


def _parse_compact_totals_block(block: str) -> ParsedIowaMetrics | None:
    first_label_match = re.search(
        r"(SPORTS WAGERING NET RECEIPTS|SPORTS WAGERING HANDLE|RETAIL NET RECEIPTS|RETAIL HANDLE|INTERNET NET RECEIPTS|INTERNET HANDLE)",
        block,
        re.IGNORECASE,
    )
    first_value_match = MONEY_RE.search(block)
    if not first_value_match or (first_label_match and first_label_match.start() < first_value_match.start()):
        return None
    values = [parse_dollar(token) for token in MONEY_RE.findall(block)]
    parsed = [value for value in values if value is not None]
    if len(parsed) < 8:
        return None
    retail_handle = parsed[4] if len(parsed) >= 5 else None
    retail_revenue = parsed[3] if len(parsed) >= 4 else None
    internet_handle = parsed[7] if len(parsed) >= 8 else None
    internet_revenue = parsed[6] if len(parsed) >= 7 else None
    return ParsedIowaMetrics(
        metrics=(parsed[1], parsed[0], retail_handle, retail_revenue, internet_handle, internet_revenue),
        layout="compact-totals",
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


def parse_iowa_pdf(data: bytes, log: Callable[[str], None] = _DEFAULT_LOG) -> ParsedIowaMetrics | None:
    text = extract_pdf_text(data, log=log)
    if not text:
        return None
    parsed = parse_totals_block(text)
    if parsed is not None:
        return parsed
    totals = parse_text_blob(text)
    if totals is None:
        return None
    return ParsedIowaMetrics(
        metrics=(totals[0], totals[1], None, None, None, None),
        layout="text-fallback",
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
                return parse_iowa_pdf(file_handle.read(), log=log)
    return None


def _is_nonempty_file(path: str | None) -> bool:
    return bool(path and os.path.isfile(path) and os.path.getsize(path) > 0)


def _load_file_bytes(path: str, log: Callable[[str], None] = _DEFAULT_LOG) -> bytes | None:
    try:
        with open(path, "rb") as file_handle:
            data = file_handle.read()
    except OSError as exc:
        log(f"WARN could not read cache {path}: {exc}")
        return None
    return data if data else None


def _save_bytes(path: str | None, data: bytes) -> None:
    if not path:
        return
    with open(path, "wb") as file_handle:
        file_handle.write(data)


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
    if _is_nonempty_file(canonical):
        log(f"INFO using cached monthly PDF for {label}")
        return _load_file_bytes(canonical, log=log)
    legacy = cache_path(cache_dir, f"iowa_{year}_{month:02d}.pdf")
    if _is_nonempty_file(legacy):
        log(f"INFO using legacy cached monthly PDF for {label}")
        data = _load_file_bytes(legacy, log=log)
        if migrate_legacy and data and canonical and canonical != legacy:
            try:
                _save_bytes(canonical, data)
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
        _save_bytes(canonical, data)
    except OSError as exc:
        log(f"WARN could not write cache {canonical}: {exc}")
    return data


def build_archive_lookup(
    months: list[tuple[int, int, str]],
    cache_dir: str | None,
    *,
    offline: bool = False,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[str, ParsedIowaMetrics]:
    archive_lookup: dict[str, ParsedIowaMetrics] = {}
    needed_fys = sorted({get_fy(month, year) for month, year, _ in months if get_fy(month, year) in FY_MEDIA_IDS})
    for fy in needed_fys:
        try:
            media_id = FY_MEDIA_IDS[fy]
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
            text = extract_pdf_text(data)
            if not text:
                log(f"WARN could not extract text from Iowa FY archive {fy[0]}-{fy[1]}")
                continue
            for month, year, label in months:
                if get_fy(month, year) != fy or label in archive_lookup:
                    continue
                parsed = parse_month_section(text, label, log=log)
                if parsed is not None:
                    archive_lookup[label] = parsed
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
    archive_lookup = build_archive_lookup(months, cache_dir, offline=offline, log=logger)
    media_lookup: dict[str, int] = dict(MEDIA_IDS)
    requested_labels = {label for _, _, label in months}
    missing_labels = requested_labels - media_lookup.keys()
    if missing_labels and not offline:
        discovered = discover_media_ids(log=logger)
        for label, media_id in discovered.items():
            media_lookup.setdefault(label, media_id)
        newly_added = sorted(set(media_lookup) - set(MEDIA_IDS))
        if newly_added:
            logger(f"INFO discovered IRGC media IDs for: {newly_added}")
    if report is not None:
        report.add_index(PUBLICATIONS_URL, anchor_count=len(media_lookup))
        for label, media_id in media_lookup.items():
            if label in requested_labels:
                report.add_anchor(f"{label}:summary", f"media/{media_id}")

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
                source_url = media_download_url(FY_MEDIA_IDS[fy_key]) if fy_key in FY_MEDIA_IDS else PUBLICATIONS_URL
            if parsed_result is None:
                data = _fetch_monthly_pdf_with_lookup(
                    year, month, label, media_lookup, cache_dir, offline=offline, log=logger,
                )
                if data:
                    parsed_result = parse_iowa_pdf(data, log=logger)
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
            validated = validate_result(label, total_handle, total_revenue)
            if validated is None:
                continue
            handle, revenue = validated
            retail_handle, retail_revenue, mobile_handle, mobile_revenue = reconcile_components(
                label,
                handle,
                revenue,
                retail_handle,
                retail_revenue,
                mobile_handle,
                mobile_revenue,
            )
            if retail_handle is not None and mobile_handle is not None:
                handle_delta_pct = abs((retail_handle + mobile_handle) - handle) / handle
                if handle_delta_pct > 0.01:
                    logger(
                        f"WARN {label}: retail/mobile handle reconciliation failed "
                        f"({handle_delta_pct * 100:.2f}% delta vs total); blanking splits"
                    )
                    retail_handle = retail_revenue = mobile_handle = mobile_revenue = None
            if (
                retail_revenue is not None
                and mobile_revenue is not None
                and retail_handle is not None
                and mobile_handle is not None
                and revenue != 0
            ):
                revenue_delta_pct = abs((retail_revenue + mobile_revenue) - revenue) / abs(revenue)
                if revenue_delta_pct > 0.01:
                    logger(
                        f"WARN {label}: retail/mobile revenue reconciliation failed "
                        f"({revenue_delta_pct * 100:.2f}% delta vs total); blanking splits"
                    )
                    retail_handle = retail_revenue = mobile_handle = mobile_revenue = None
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
            hold_val = revenue / handle * 100 if handle else 0
            logger(
                f"INFO {label}: handle={handle:,.0f} revenue={revenue:,.0f} "
                f"retail_handle={retail_handle if retail_handle is not None else 'n/a'} "
                f"mobile_handle={mobile_handle if mobile_handle is not None else 'n/a'} "
                f"hold={hold_val:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
