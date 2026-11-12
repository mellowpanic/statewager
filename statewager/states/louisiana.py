"""Louisiana sports wagering regulatory fetcher and parser (LGCB / LSP)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import os
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, load_cached_or_fetch
from ..discover import (
    DiscoveryReport,
    classify_cumulative_filename,
    classify_sportsbook_filename,
    crawl_anchors,
)
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

STATE_NAME = "Louisiana"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = False

USER_AGENT = "Mozilla/5.0"
REPORTS_URL = (
    "https://lsp.org/about/leadershipsections/bureau-of-investigations/"
    "gaming-enforcement-division/gaming-revenue-reports/"
)
_DEFAULT_LOG = build_state_logger("LA")
_LA_CACHE_FILE_RE = re.compile(r"^louisiana_(20\d{2})_(0[1-9]|1[0-2])_(mobile|retail)\.xlsx$", re.IGNORECASE)


def discover_remote_links(
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Return ``(per-month xlsx urls, cumulative xlsx urls per channel-label)``."""
    links = crawl_anchors(REPORTS_URL, log=log)
    if not links:
        return {}, {}
    if report is not None:
        report.add_index(REPORTS_URL, anchor_count=len(links))
    per_month: dict[str, dict[str, list[str]]] = {}
    cumulative: dict[str, list[tuple[int, str]]] = {}
    for link in links:
        href = unescape(link.get("href", "").strip())
        if ".xlsx" not in href.lower():
            if report is not None and href:
                report.add_classification(href, title=link.get("title", ""), text=link.get("text", ""), reason="not-xlsx")
            continue
        full_url = urljoin(REPORTS_URL, href)
        title = link.get("title", "")
        text = link.get("text", "")
        classification = classify_sportsbook_filename(full_url, title=title, text=text)
        if classification:
            label, channel = classification
            urls = per_month.setdefault(label, {}).setdefault(channel, [])
            if full_url not in urls:
                urls.append(full_url)
            if report is not None:
                report.add_anchor(f"{label}:{channel}", full_url)
            continue
        cum_class = classify_cumulative_filename(full_url, title=title, text=text)
        if cum_class:
            months, channel = cum_class
            max_year = max(int(month.split()[-1]) for month in months)
            for month_label in months:
                cumulative.setdefault(f"{month_label}:{channel}", []).append((max_year, full_url))
            if report is not None:
                report.add_anchor(f"cumulative:{channel}", full_url)
            continue
        if report is not None:
            report.add_classification(full_url, title=title, text=text, reason="no-sportsbook-month")
    cumulative_sorted: dict[str, list[str]] = {}
    for key, entries in cumulative.items():
        seen: set[str] = set()
        ordered: list[str] = []
        for _, url in sorted(entries, key=lambda pair: -pair[0]):
            if url not in seen:
                seen.add(url)
                ordered.append(url)
        cumulative_sorted[key] = ordered
    return per_month, cumulative_sorted


def parse_workbook_all_months(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[str, tuple[float, float]]:
    """Return ``{ "Mon YYYY": (handle, revenue) }`` for every populated row."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Louisiana workbooks")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open Louisiana workbook: {exc}")
        return {}
    results: dict[str, tuple[float, float]] = {}
    try:
        ordered_sheets = sorted(
            workbook.worksheets,
            key=lambda sheet: 0 if any(token in sheet.title.lower() for token in ("current", "mobile", "retail")) else 1,
        )
        for sheet in ordered_sheets:
            for row in sheet.iter_rows(values_only=True):
                month_cell = row[0] if len(row) > 0 else None
                handle = row[2] if len(row) > 2 else None
                revenue = row[4] if len(row) > 4 else None
                if not isinstance(month_cell, (datetime, date)):
                    continue
                if not isinstance(handle, (int, float)) or not isinstance(revenue, (int, float)):
                    continue
                if handle == 0 or handle == -1:
                    continue
                label = month_cell.strftime("%b %Y")
                results.setdefault(label, (float(handle), float(revenue)))
    finally:
        workbook.close()
    return results


def _load_one(
    url_or_path: str,
    cache_dir: str | None,
    label_hint: str,
    *,
    offline: bool = False,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    if not url_or_path.startswith("http"):
        try:
            with open(url_or_path, "rb") as file_handle:
                return file_handle.read()
        except Exception as exc:
            log(f"WARN could not read local file {url_or_path}: {exc}")
            return None
    cache_filename = re.sub(r"[^A-Za-z0-9._-]", "_", f"{label_hint}_{os.path.basename(urlparse(url_or_path).path)}")
    cache_file = cache_path(cache_dir, cache_filename)

    timestamp = None
    label_match = re.search(r"([A-Za-z]{3})[\s\-_]+(\d{4})", label_hint)
    if label_match:
        try:
            timestamp = datetime.strptime(
                label_match.group(0).replace("_", " ").replace("-", " "), "%b %Y",
            ).strftime("%Y%m")
        except ValueError:
            timestamp = None

    def _fetcher() -> bytes | None:
        return fetch_with_wayback_fallback(
            url_or_path,
            timestamp_yyyymm=timestamp,
            signature_check=b"PK",
            log=log,
        )

    return load_cached_or_fetch(
        cache_file=cache_file,
        fetcher=_fetcher,
        log=log,
        cache_hit_message=f"INFO using cached source for {label_hint}: {cache_filename}",
        fetch_success_message=f"INFO downloaded {label_hint}: {url_or_path}",
        fetch_error_message=f"could not download {label_hint} from {url_or_path}",
        validate=lambda payload: payload.startswith(b"PK"),
        offline=offline,
    )


def _scan_local_dir(local_dir: str) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    per_month: dict[str, dict[str, list[str]]] = {}
    cumulative: dict[str, list[str]] = {}
    for entry in sorted(os.listdir(local_dir)):
        if not entry.lower().endswith(".xlsx"):
            continue
        full_path = os.path.join(local_dir, entry)
        classification = classify_sportsbook_filename(entry)
        if classification:
            label, channel = classification
            per_month.setdefault(label, {}).setdefault(channel, []).append(full_path)
            continue
        cum_class = classify_cumulative_filename(entry)
        if cum_class:
            months, channel = cum_class
            for month_label in months:
                cumulative.setdefault(f"{month_label}:{channel}", []).append(full_path)
    return per_month, cumulative


def _scan_cache_dir(
    cache_dir: str,
    target_labels: set[str],
) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Discover cached Louisiana workbooks without crawling remote indices.

    Per-month ``louisiana_YYYY_MM_{channel}.xlsx`` files match their exact month
    first, and are also indexed newest-first as cumulative fallbacks for older
    months present inside the rolling FY workbooks.
    """
    per_month: dict[str, dict[str, list[str]]] = {}
    cumulative: dict[str, list[str]] = {}
    if not os.path.isdir(cache_dir):
        return per_month, cumulative
    dated_channel_files: dict[str, list[tuple[tuple[int, int], str]]] = {"mobile": [], "retail": []}
    for entry in sorted(os.listdir(cache_dir)):
        if not entry.lower().endswith(".xlsx"):
            continue
        full_path = os.path.join(cache_dir, entry)
        la_match = _LA_CACHE_FILE_RE.match(entry)
        if la_match:
            year = int(la_match.group(1))
            month_num = int(la_match.group(2))
            channel = la_match.group(3).lower()
            label = datetime(year, month_num, 1).strftime("%b %Y")
            per_month.setdefault(label, {}).setdefault(channel, []).append(full_path)
            dated_channel_files.setdefault(channel, []).append(((year, month_num), full_path))
            continue
        classification = classify_sportsbook_filename(entry)
        if classification:
            label, channel = classification
            per_month.setdefault(label, {}).setdefault(channel, []).append(full_path)
            continue
        cum_class = classify_cumulative_filename(entry)
        if cum_class:
            months, channel = cum_class
            for month_label in months:
                cumulative.setdefault(f"{month_label}:{channel}", []).append(full_path)

    for channel, items in dated_channel_files.items():
        newest_first = [path for _, path in sorted(items, key=lambda pair: pair[0], reverse=True)]
        if not newest_first:
            continue
        for label in target_labels:
            bucket = cumulative.setdefault(f"{label}:{channel}", [])
            for path in newest_first:
                if path not in bucket:
                    bucket.append(path)
    return per_month, cumulative


def resolve_month_data(
    target_labels: set[str],
    local_dir: str | None,
    cache_dir: str | None,
    *,
    offline: bool = False,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[dict[tuple[str, str], tuple[float, float]], dict[tuple[str, str], str]]:
    """Return ``({(label, channel): (handle, revenue)}, {(label, channel): source})``."""
    local_per_month: dict[str, dict[str, list[str]]] = {}
    local_cumulative: dict[str, list[str]] = {}
    if local_dir:
        local_per_month, local_cumulative = _scan_local_dir(local_dir)

    cached_per_month: dict[str, dict[str, list[str]]] = {}
    cached_cumulative: dict[str, list[str]] = {}
    if cache_dir:
        cached_per_month, cached_cumulative = _scan_cache_dir(cache_dir, target_labels)

    remote_per_month: dict[str, dict[str, list[str]]] = {}
    remote_cumulative: dict[str, list[str]] = {}
    if not offline:
        remote_per_month, remote_cumulative = discover_remote_links(report=report, log=log)
        log(
            "INFO discovered remote XLSX: "
            f"per_month={sum(len(v) for v in remote_per_month.values())} entries across "
            f"{len(remote_per_month)} months; cumulative={len(remote_cumulative)} (month, channel) keys"
        )

    resolved: dict[tuple[str, str], tuple[float, float]] = {}
    resolved_sources: dict[tuple[str, str], str] = {}
    workbook_cache: dict[str, dict[str, tuple[float, float]]] = {}

    def load_and_parse(source: str, label_hint: str) -> dict[str, tuple[float, float]]:
        if source in workbook_cache:
            return workbook_cache[source]
        data = _load_one(source, cache_dir, label_hint, offline=offline, log=log)
        if data is None:
            workbook_cache[source] = {}
            return {}
        parsed = parse_workbook_all_months(data, log=log)
        workbook_cache[source] = parsed
        return parsed

    for label in sorted(target_labels):
        for channel in ("mobile", "retail"):
            sources: list[str] = []
            sources.extend(local_per_month.get(label, {}).get(channel, []))
            sources.extend(local_cumulative.get(f"{label}:{channel}", []))
            if offline:
                sources.extend(cached_per_month.get(label, {}).get(channel, []))
                sources.extend(cached_cumulative.get(f"{label}:{channel}", []))
            else:
                sources.extend(remote_per_month.get(label, {}).get(channel, []))
                sources.extend(cached_per_month.get(label, {}).get(channel, []))
                sources.extend(remote_cumulative.get(f"{label}:{channel}", []))
                sources.extend(cached_cumulative.get(f"{label}:{channel}", []))
            seen: set[str] = set()
            ordered_sources = [src for src in sources if not (src in seen or seen.add(src))]
            if not ordered_sources:
                log(f"WARN {label} {channel}: no candidate source found")
                if report is not None:
                    report.add_probe(f"{label}:{channel}", "(none)", reason="no-candidate-source")
                continue
            for source in ordered_sources:
                parsed = load_and_parse(source, f"{label}-{channel}")
                if label in parsed:
                    resolved[(label, channel)] = parsed[label]
                    resolved_sources[(label, channel)] = source
                    log(
                        f"INFO {label} {channel}: handle={parsed[label][0]:,.0f} "
                        f"revenue={parsed[label][1]:,.0f} via "
                        f"{os.path.basename(urlparse(source).path) if source.startswith('http') else source}"
                    )
                    if report is not None:
                        report.add_match(f"{label}:{channel}", source)
                        report.add_probe(f"{label}:{channel}", source, status="match", reason="row-found")
                    break
                else:
                    if report is not None:
                        report.add_probe(f"{label}:{channel}", source, reason="month-row-missing")
            else:
                tried = ", ".join(
                    os.path.basename(urlparse(src).path) if src.startswith("http") else os.path.basename(src)
                    for src in ordered_sources
                )
                log(f"WARN {label} {channel}: data not present in candidate workbooks ({tried})")
    return resolved, resolved_sources


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
    """Fetch and parse Louisiana monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Louisiana sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    months = [(mdate.month, mdate.year, mdate.strftime("%b %Y")) for mdate in target_dates]
    target_labels = {label for _, _, label in months}
    resolved, resolved_sources = resolve_month_data(
        target_labels, local_dir, cache_dir, offline=offline, report=report, log=logger,
    )
    results: list[StateMonthResult] = []
    for month, year, label in months:
        iso_month = format_iso_month(year, month)
        try:
            mobile_values = resolved.get((label, "mobile"))
            retail_values = resolved.get((label, "retail"))
            if not mobile_values or not retail_values:
                log_skip(logger, label, f"missing pair mobile={bool(mobile_values)} retail={bool(retail_values)}")
                continue
            mobile_handle, mobile_revenue = mobile_values
            retail_handle, retail_revenue = retail_values
            total_handle = mobile_handle + retail_handle
            total_revenue = mobile_revenue + retail_revenue
            validated = validate_result(label, total_handle, total_revenue)
            if validated is None:
                continue
            handle, revenue = validated
            hold = hold_pct(handle, revenue)
            retail_hold = hold_pct(retail_handle, retail_revenue) if retail_handle else None
            mobile_hold = hold_pct(mobile_handle, mobile_revenue) if mobile_handle else None
            m_src = resolved_sources.get((label, "mobile"), "")
            source_url = m_src if m_src.startswith("http") else REPORTS_URL
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
            logger(
                f"INFO {label}: handle={handle:,.0f} revenue={revenue:,.0f} "
                f"retail_handle={retail_handle:,.0f} mobile_handle={mobile_handle:,.0f} hold={hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
