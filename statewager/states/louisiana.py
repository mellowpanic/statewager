"""Louisiana sports wagering regulatory fetcher and parser (LGCB / LSP)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import os
import re
from typing import Callable
from urllib.parse import urljoin, urlparse

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, load_cached_or_fetch, recent_fy_cache_ttl
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
    finalize_state_month,
    fiscal_year_july,
    format_iso_month,
    log_month_result,
    log_skip,
    month_yyyymm,
    parse_month_spec,
    resolve_fetch_months,
    sum_optional,
)
from ..parse import is_number, open_xlsx_workbook

STATE_NAME = "Louisiana"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = False

REPORTS_URL = (
    "https://lsp.org/about/leadershipsections/bureau-of-investigations/"
    "gaming-enforcement-division/gaming-revenue-reports/"
)
_DEFAULT_LOG = build_state_logger("LA")
_LA_CACHE_FILE_RE = re.compile(r"^louisiana_(20\d{2})_(0[1-9]|1[0-2])_(mobile|retail)\.xlsx$", re.IGNORECASE)
_CACHE_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_LABEL_HINT_RE = re.compile(r"([A-Za-z]{3})[\s\-_]+(\d{4})")


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


def _parse_workbook_all_months_core(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float | None, float | None]]]:
    """Return ``({label: (handle, revenue)}, {label: (promo_deductions, tax)})``."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Louisiana workbooks")
    results: dict[str, tuple[float, float]] = {}
    fiscal_results: dict[str, tuple[float | None, float | None]] = {}
    with open_xlsx_workbook(data, openpyxl_mod=openpyxl, log=log, error_prefix="could not open Louisiana workbook") as workbook:
        if workbook is None:
            return {}, {}
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
                if not is_number(handle) or not is_number(revenue) or handle in (0, -1):
                    continue
                label = month_cell.strftime("%b %Y")
                if label not in results:
                    results[label] = (float(handle), float(revenue))
                    promo = abs(float(row[3])) if len(row) > 3 and is_number(row[3]) else None
                    tax = float(row[5]) if len(row) > 5 and is_number(row[5]) else None
                    fiscal_results[label] = (promo, tax)
    return results, fiscal_results


def parse_workbook_fiscal_all_months(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[str, tuple[float | None, float | None]]:
    """Return ``{ "Mon YYYY": (promo_deductions, tax) }`` for every populated row."""
    _, fiscal_results = _parse_workbook_all_months_core(data, log=log)
    return fiscal_results


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
    cache_filename = _CACHE_SAFE_RE.sub("_", f"{label_hint}_{os.path.basename(urlparse(url_or_path).path)}")
    cache_file = cache_path(cache_dir, cache_filename)
    label_match = _LABEL_HINT_RE.search(label_hint)
    timestamp = month_yyyymm(label_match.group(0).replace("_", " ").replace("-", " ")) if label_match else None
    ttl_seconds = (
        recent_fy_cache_ttl(fiscal_year_july(int(timestamp[:4]), int(timestamp[4:6]))[1])
        if timestamp
        else None
    )

    return load_cached_or_fetch(
        cache_file=cache_file,
        fetcher=lambda: fetch_with_wayback_fallback(
            url_or_path,
            timestamp_yyyymm=timestamp,
            signature_check=b"PK",
            log=log,
        ),
        log=log,
        cache_hit_message=f"INFO using cached source for {label_hint}: {cache_filename}",
        fetch_success_message=f"INFO downloaded {label_hint}: {url_or_path}",
        fetch_error_message=f"could not download {label_hint} from {url_or_path}",
        validate=lambda payload: payload.startswith(b"PK"),
        cache_ttl_seconds=ttl_seconds,
        offline=offline,
    )


def _index_xlsx_entry(
    entry: str,
    full_path: str,
    per_month: dict[str, dict[str, list[str]]],
    cumulative: dict[str, list[str]],
) -> tuple[str, str] | None:
    classification = classify_sportsbook_filename(entry)
    if classification:
        label, channel = classification
        per_month.setdefault(label, {}).setdefault(channel, []).append(full_path)
        return label, channel
    cum_class = classify_cumulative_filename(entry)
    if cum_class:
        months, channel = cum_class
        for month_label in months:
            cumulative.setdefault(f"{month_label}:{channel}", []).append(full_path)
    return None


def _scan_local_dir(local_dir: str) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    per_month: dict[str, dict[str, list[str]]] = {}
    cumulative: dict[str, list[str]] = {}
    if not os.path.isdir(local_dir):
        return per_month, cumulative
    for entry in sorted(os.listdir(local_dir)):
        if entry.lower().endswith(".xlsx"):
            _index_xlsx_entry(entry, os.path.join(local_dir, entry), per_month, cumulative)
    return per_month, cumulative


def _scan_cache_dir(
    cache_dir: str,
    target_labels: set[str],
) -> tuple[dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    """Discover cached Louisiana workbooks without crawling remote indices."""
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
            year, month_num = int(la_match.group(1)), int(la_match.group(2))
            channel = la_match.group(3).lower()
            label = datetime(year, month_num, 1).strftime("%b %Y")
            per_month.setdefault(label, {}).setdefault(channel, []).append(full_path)
            dated_channel_files.setdefault(channel, []).append(((year, month_num), full_path))
            continue
        cls = _index_xlsx_entry(entry, full_path, per_month, cumulative)
        if cls is not None:
            label, channel = cls
            try:
                month_dt = parse_month_spec(label)
                dated_channel_files.setdefault(channel, []).append(((month_dt.year, month_dt.month), full_path))
            except ValueError:
                pass

    for channel, items in dated_channel_files.items():
        newest_first = [path for _, path in sorted(items, key=lambda pair: pair[0], reverse=True)]
        if not newest_first:
            continue
        for label in target_labels:
            bucket = cumulative.setdefault(f"{label}:{channel}", [])
            seen = set(bucket)
            for path in newest_first:
                if path not in seen:
                    bucket.append(path)
                    seen.add(path)
    return per_month, cumulative


def _resolve_month_data_with_fiscal(
    target_labels: set[str],
    local_dir: str | None,
    cache_dir: str | None,
    *,
    offline: bool = False,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    dict[tuple[str, str], tuple[float, float]],
    dict[tuple[str, str], str],
    dict[tuple[str, str], tuple[float | None, float | None]],
]:
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
    resolved_fiscal: dict[tuple[str, str], tuple[float | None, float | None]] = {}
    workbook_cache: dict[str, tuple[dict[str, tuple[float, float]], dict[str, tuple[float | None, float | None]]]] = {}

    def load_and_parse(
        source: str, label_hint: str,
    ) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float | None, float | None]]]:
        if source in workbook_cache:
            return workbook_cache[source]
        data = _load_one(source, cache_dir, label_hint, offline=offline, log=log)
        if data is None:
            workbook_cache[source] = ({}, {})
            return {}, {}
        parsed, fiscal = _parse_workbook_all_months_core(data, log=log)
        workbook_cache[source] = (parsed, fiscal)
        return parsed, fiscal

    for label in sorted(target_labels, key=parse_month_spec):
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
                parsed, fiscal = load_and_parse(source, f"{label}-{channel}")
                if label in parsed:
                    resolved[(label, channel)] = parsed[label]
                    resolved_sources[(label, channel)] = source
                    if label in fiscal:
                        resolved_fiscal[(label, channel)] = fiscal[label]
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
    return resolved, resolved_sources, resolved_fiscal


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
    resolved, resolved_sources, resolved_fiscal = _resolve_month_data_with_fiscal(
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
            m_promo, m_tax = resolved_fiscal.get((label, "mobile"), (None, None))
            r_promo, r_tax = resolved_fiscal.get((label, "retail"), (None, None))
            total_promo = sum_optional(m_promo, r_promo)
            total_gross = (total_revenue + total_promo) if total_promo is not None else None
            total_tax = sum_optional(m_tax, r_tax)
            m_src = resolved_sources.get((label, "mobile"), "")
            source_url = m_src if m_src.startswith("http") else REPORTS_URL
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
                reconcile_splits=False,
                gross_revenue=total_gross,
                promo_deductions=total_promo,
                tax=total_tax,
                log=logger,
                label=label,
            )
            if item is None:
                continue
            results.append(item)
            log_month_result(logger, label, item)
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
