"""Pennsylvania sports wagering regulatory fetcher and parser (PGCB)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import os
import re
from typing import Callable, Iterator, List, Optional, Tuple
from urllib.parse import urljoin

try:
    import openpyxl
except ImportError:
    openpyxl = None

from dateutil.relativedelta import relativedelta

from ..cache import cache_path, find_local_file, load_cached_or_fetch, recent_fy_cache_ttl, refresh_cached_if_stale
from ..discover import DiscoveryReport, crawl_anchors, parse_anchors
from ..http import fetch_bytes, fetch_text, fetch_with_wayback_fallback
from ..model import (
    MONTH_ABBR,
    MONTH_NAMES,
    OperatorDetailTuple,
    OperatorFiscalTuple,
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    fiscal_year_july,
    format_iso_month,
    log_month_result,
    log_skip,
    resolve_fetch_months,
    sum_optional,
)
from ..parse import XLSX_MAGIC, clean_lower, is_number, is_xlsx_bytes, open_xlsx_workbook

PaDetailRow = OperatorDetailTuple
PaMonthCore = Tuple[float, float, Optional[float], Optional[float], Optional[float], Optional[float], List[PaDetailRow]]
PaFiscalTuple = OperatorFiscalTuple

STATE_NAME = "Pennsylvania"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
PGCB_BASE = "https://gamingcontrolboard.pa.gov"
REVENUE_PAGE = f"{PGCB_BASE}/news-and-transparency/revenue"
FILTER_IDS = {(2026, 2027): 292, (2025, 2026): 285, (2024, 2025): 252, (2023, 2024): 115}
_FY_OPTION_RE = re.compile(
    r'<option[^>]*value=["\'](\d+)["\'][^>]*>\s*FY\s*(20\d{2})\s*/\s*(20\d{2})\s*</option>',
    re.IGNORECASE,
)
_FY_TEXT_RE = re.compile(r"fy[ _-]?(?:20)?(\d{2})[ _-]?(?:20)?(\d{2})(?!\d)")

_DEFAULT_LOG = build_state_logger("PA")


def _absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith(("http://", "https://")):
        return href
    return urljoin(REVENUE_PAGE, href)


def parse_filter_options(html: str) -> dict[tuple[int, int], int]:
    """Parse PGCB fiscal-year select options into ``{(fy_start, fy_end): filter_id}``."""
    discovered: dict[tuple[int, int], int] = {}
    for match in _FY_OPTION_RE.finditer(html):
        filter_id, fy_start, fy_end = int(match.group(1)), int(match.group(2)), int(match.group(3))
        discovered[(fy_start, fy_end)] = filter_id
    return discovered


def crawl_revenue_index(
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
    target_fys: set[tuple[int, int]] | list[tuple[int, int]] | None = None,
) -> list[dict[str, str]]:
    """Crawl PGCB revenue page once, optionally across needed FY filter views."""
    filter_ids = dict(FILTER_IDS)
    try:
        index_html = fetch_text(REVENUE_PAGE, timeout=HTTP_TIMEOUT, retries=HTTP_RETRIES)
    except Exception as exc:
        log(f"WARN could not fetch anchor index {REVENUE_PAGE}: {exc}")
        index_html = None
    if index_html:
        for fy_key, fid in parse_filter_options(index_html).items():
            filter_ids.setdefault(fy_key, fid)
    if target_fys is not None:
        target_set = set(target_fys)
        filter_ids = {k: v for k, v in filter_ids.items() if k in target_set}
    seen_hrefs: set[str] = set()
    aggregated: list[dict[str, str]] = []

    def _collect(links: list[dict[str, str]], page_url: str) -> None:
        if not links:
            return
        if report is not None:
            report.add_index(page_url, anchor_count=len(links))
        for link in links:
            href = unescape(link.get("href", "").strip())
            if not href:
                continue
            abs_href = _absolute_url(href)
            if abs_href in seen_hrefs:
                continue
            seen_hrefs.add(abs_href)
            aggregated.append({
                "href": abs_href,
                "title": link.get("title", ""),
                "text": link.get("text", ""),
            })

    if index_html:
        _collect(parse_anchors(index_html, url=REVENUE_PAGE, log=log), REVENUE_PAGE)
    for (_fy_start, _fy_end), filter_id in sorted(filter_ids.items(), reverse=True):
        url = f"{REVENUE_PAGE}?field_gaming_revenue_fiscal_year_target_id={filter_id}"
        _collect(crawl_anchors(url, log=log), url)
    return aggregated


def _is_fy_summary_anchor(
    href: str, title: str, text: str,
) -> tuple[int, int] | None:
    lower_path = href.split("?", 1)[0].split("#", 1)[0].lower()
    if not lower_path.endswith(".xlsx"):
        return None
    combined = " ".join(part for part in (href, title, text) if part).lower()
    fy_match = _FY_TEXT_RE.search(combined)
    if not fy_match:
        return None
    if "sports wagering" in combined and ("monthly" in combined or "summary" in combined):
        return (2000 + int(fy_match.group(1)), 2000 + int(fy_match.group(2)))
    return None


def discover_fy_link(
    fy_start: int,
    fy_end: int,
    *,
    anchors: list[dict[str, str]] | None = None,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> str | None:
    """Locate the FY rolling XLSX URL via anchor crawl + URL fallbacks."""
    fy_token = f"FY{fy_start % 100:02d}-{fy_end % 100:02d}"
    fy_token_lower = fy_token.lower()
    if anchors is None:
        anchors = crawl_revenue_index(report=report, log=log, target_fys={(fy_start, fy_end)})

    fallback_sport_href: str | None = None
    for anchor in anchors:
        href = anchor["href"]
        fy_pair = _is_fy_summary_anchor(href, anchor["title"], anchor["text"])
        if fy_pair is not None:
            if fy_pair == (fy_start, fy_end) or any(
                fy_token_lower in field.lower() for field in (href, anchor["title"], anchor["text"])
            ):
                return href
        elif fallback_sport_href is None:
            lower_href = href.lower()
            if lower_href.endswith(".xlsx") and fy_token_lower in lower_href and "sport" in lower_href:
                fallback_sport_href = href
    if fallback_sport_href is not None:
        return fallback_sport_href

    today = date.today().replace(day=1)
    min_pub = date(fy_start, 7, 1)
    max_pub = min(today, date(fy_end, 9, 1))
    patterns = [
        "{base}/sites/default/files/{published:%Y-%m}/{fy_token}%20Monthly%20Sports%20Wagering%20Report%20Summary{suffix}",
        "{base}/sites/default/files/{published:%Y-%m}/{fy_token}-Monthly-Sports-Wagering-Report-Summary{suffix}",
        "{base}/sites/default/files/{published:%Y-%m}/{fy_token}_Monthly_Sports_Wagering_Report_Summary{suffix}",
        "{base}/sites/default/files/{published:%Y-%m}/{fy_token}%20Sports%20Wagering%20Report%20Summary{suffix}",
    ]
    suffixes = (".xlsx", "%20.xlsx")
    published = max_pub
    while published >= min_pub:
        for pattern in patterns:
            for suffix in suffixes:
                guessed = pattern.format(
                    base=PGCB_BASE,
                    published=published,
                    fy_token=fy_token,
                    suffix=suffix,
                )
                if report is not None:
                    report.add_probe(f"FY{fy_start % 100:02d}-{fy_end % 100:02d}", guessed, reason="probe")
                try:
                    data = fetch_bytes(guessed, retries=1, timeout=10)
                except Exception as exc:
                    log(f"WARN could not probe {guessed}: {exc}")
                    continue
                if is_xlsx_bytes(data):
                    return guessed
        published -= relativedelta(months=1)
    return None


def download_fy_workbook(
    fy_start: int,
    fy_end: int,
    *,
    anchors: list[dict[str, str]] | None = None,
    report: DiscoveryReport | None = None,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> bytes | None:
    link = discover_fy_link(fy_start, fy_end, anchors=anchors, report=report, log=log)
    if not link:
        log(
            f"WARN no FY{fy_start % 100:02d}-{fy_end % 100:02d} workbook discovered "
            f"on {REVENUE_PAGE}"
        )
        return None
    timestamp = f"{fy_end}06"
    data = fetch_with_wayback_fallback(
        link, timestamp_yyyymm=timestamp, signature_check=XLSX_MAGIC, log=log,
    )
    if data is None:
        return None
    log(f"INFO downloaded {link}")
    if report is not None:
        report.add_match(f"FY{fy_start % 100:02d}-{fy_end % 100:02d}", link)
    return data


def extract_section_metrics(
    rows: list[tuple[object, ...]],
    start_index: int,
    col_to_label: dict[int, str],
    revenue_labels: tuple[str, ...],
) -> dict[str, tuple[float, float]]:
    handle_row = None
    revenue_row = None
    for row in rows[start_index + 1:start_index + 8]:
        label = clean_lower(row[0] if row else "")
        if not label:
            continue
        if handle_row is None and label.startswith("handle"):
            handle_row = row
        if revenue_row is None and label in revenue_labels:
            revenue_row = row
        if handle_row is not None and revenue_row is not None:
            break
    if handle_row is None or revenue_row is None:
        return {}
    results: dict[str, tuple[float, float]] = {}
    for column, label in col_to_label.items():
        handle = handle_row[column] if column < len(handle_row) else None
        revenue = revenue_row[column] if column < len(revenue_row) else None
        if is_number(handle) and is_number(revenue):
            results[label] = (float(handle), float(revenue))
    return results


_MONTH_HEADER_PATTERNS: tuple[tuple[int, str, re.Pattern[str]], ...] = tuple(
    (
        month_index,
        abbr,
        re.compile(
            rf"\b(?:{MONTH_NAMES[month_index]}|{'Sept|' if month_index == 9 else ''}{abbr})\b",
            re.IGNORECASE,
        ),
    )
    for month_index, abbr in MONTH_ABBR.items()
)


def parse_month_columns(rows: list[tuple[object, ...]], fy_start: int, fy_end: int) -> dict[int, str]:
    header_row = None
    for index, row in enumerate(rows[:12]):
        values = " ".join(str(cell or "") for cell in row).lower()
        if "july" in values and "january" in values:
            header_row = index
            break
    if header_row is None:
        return {}
    col_to_label: dict[int, str] = {}
    for column, cell in enumerate(rows[header_row]):
        text = str(cell or "").strip()
        if not text or "ytd" in text.lower() or "total" in text.lower():
            continue
        for month_index, month, pattern in _MONTH_HEADER_PATTERNS:
            if pattern.search(text):
                year = fy_end if month_index <= 6 else fy_start
                col_to_label[column] = f"{month} {year}"
                break
    return col_to_label


def parse_section_totals(
    rows: list[tuple[object, ...]],
    fy_start: int,
    fy_end: int,
    section_label: str,
    revenue_labels: tuple[str, ...],
    *,
    col_to_label: dict[int, str] | None = None,
) -> dict[str, tuple[float, float]]:
    if col_to_label is None:
        col_to_label = parse_month_columns(rows, fy_start, fy_end)
    if not col_to_label:
        return {}
    target = clean_lower(section_label)
    for index, row in enumerate(rows):
        if clean_lower(row[0] if row else "") == target:
            results = extract_section_metrics(rows, index, col_to_label, revenue_labels)
            if results:
                return results
    return {}


def parse_sheet_totals(
    rows: list[tuple[object, ...]],
    fy_start: int,
    fy_end: int,
    *,
    col_to_label: dict[int, str] | None = None,
) -> dict[str, tuple[float, float]]:
    return parse_section_totals(
        rows,
        fy_start,
        fy_end,
        "Grand Total",
        ("revenue", "gross revenue (taxable)"),
        col_to_label=col_to_label,
    )


def classify_sheet(title: str) -> str | None:
    lower = title.lower()
    if "retail" in lower:
        return "retail"
    if "online" in lower or "mobile" in lower:
        return "mobile"
    return None


def _to_float(value: object) -> float | None:
    return float(value) if is_number(value) else None


def _cell(row: tuple[object, ...] | None, column: int) -> object:
    return row[column] if row and column < len(row) else None


_SKIP_BLOCK_LABELS: frozenset[str] = frozenset({
    "GRAND TOTAL",
    "TOTAL SPORTS WAGERING",
    "RETAIL SPORTS WAGERING",
    "ONLINE SPORTS WAGERING",
})


def _block_end(rows: list[tuple[object, ...]], start_index: int) -> int:
    """Find the exclusive end index of a PGCB summary or operator block."""
    for idx in range(start_index + 4, len(rows)):
        row_label = clean_lower(rows[idx][0] if rows[idx] else "")
        if row_label == "grand total":
            return idx
        if idx + 1 < len(rows) and clean_lower(rows[idx + 1][0] if rows[idx + 1] else "") == "total sports wagering":
            return idx
    return len(rows)


def _iter_operator_blocks(rows: list[tuple[object, ...]]) -> Iterator[tuple[int, str]]:
    """Yield ``(start_index, operator_label)`` for each PGCB operator block."""
    index = 0
    while index + 3 < len(rows):
        label = str((rows[index][0] if rows[index] else None) or "").strip()
        if not label or label.upper() in _SKIP_BLOCK_LABELS:
            index += 1
            continue
        if (
            clean_lower(rows[index + 1][0] if rows[index + 1] else "") != "total sports wagering"
            or clean_lower(rows[index + 2][0] if rows[index + 2] else "") != "handle*"
            or clean_lower(rows[index + 3][0] if rows[index + 3] else "") != "revenue"
        ):
            index += 1
            continue
        yield index, label
        index = _block_end(rows, index)


def parse_detail_metrics(
    rows: list[tuple[object, ...]],
    fy_start: int,
    fy_end: int,
    *,
    col_to_label: dict[int, str] | None = None,
    _op_blocks: list[tuple[int, str]] | None = None,
) -> dict[str, list[PaDetailRow]]:
    if col_to_label is None:
        col_to_label = parse_month_columns(rows, fy_start, fy_end)
    if not col_to_label:
        return {}
    details: dict[str, list[PaDetailRow]] = {label: [] for label in col_to_label.values()}
    for index, label in (_op_blocks if _op_blocks is not None else _iter_operator_blocks(rows)):
        end = _block_end(rows, index)
        handle_row = rows[index + 2]
        revenue_row = rows[index + 3]
        retail_handle_row = None
        retail_revenue_row = None
        mobile_handle_row = None
        mobile_revenue_row = None
        subsection: str | None = None
        for row in rows[index + 4:end]:
            row_label = clean_lower(row[0] if row else "")
            if not row_label:
                continue
            if row_label == "retail sports wagering":
                subsection = "retail"
            elif row_label == "online sports wagering":
                subsection = "online"
            elif subsection == "retail":
                if retail_handle_row is None and row_label == "handle*":
                    retail_handle_row = row
                elif retail_revenue_row is None and row_label in {"gross revenue (taxable)", "revenue"}:
                    retail_revenue_row = row
            elif subsection == "online":
                if mobile_handle_row is None and row_label == "handle*":
                    mobile_handle_row = row
                elif mobile_revenue_row is None and row_label in {"gross revenue (taxable)", "revenue"}:
                    mobile_revenue_row = row
        for column, month_label in col_to_label.items():
            handle = _cell(handle_row, column)
            revenue = _cell(revenue_row, column)
            if is_number(handle) and is_number(revenue) and (handle != 0 or revenue != 0):
                details.setdefault(month_label, []).append(
                    (
                        label,
                        float(handle),
                        float(revenue),
                        _to_float(_cell(retail_handle_row, column)),
                        _to_float(_cell(retail_revenue_row, column)),
                        _to_float(_cell(mobile_handle_row, column)),
                        _to_float(_cell(mobile_revenue_row, column)),
                    )
                )
    return details


def _sum_channel_metrics(
    details: dict[str, list[PaDetailRow]],
    handle_idx: int,
    rev_idx: int,
) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for month_label, month_details in details.items():
        total_h = 0.0
        total_r = 0.0
        found = False
        for entry in month_details:
            h_val = entry[handle_idx]
            r_val = entry[rev_idx]
            if h_val is not None and r_val is not None:
                total_h += float(h_val)
                total_r += float(r_val)
                found = True
        if found:
            out[month_label] = (total_h, total_r)
    return out


def aggregate_detail_totals(
    details: dict[str, list[PaDetailRow]],
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    return _sum_channel_metrics(details, 3, 4), _sum_channel_metrics(details, 5, 6)


def _extract_fiscal_block(
    rows: list[tuple[object, ...]],
    start_index: int,
    col_to_label: dict[int, str],
) -> dict[str, PaFiscalTuple]:
    """Extract ``{month_label: (gross_revenue, promo_deductions, tax)}`` from a PA summary/operator block."""
    rev_row = None
    promo_row = None
    state_tax_row = None
    lsa_row = None
    for offset, row in enumerate(rows[start_index + 1:_block_end(rows, start_index)], start=1):
        label = clean_lower(row[0] if row else "")
        if not label:
            continue
        if (offset > 1 and label == "total sports wagering") or label in ("retail sports wagering", "online sports wagering"):
            break
        if rev_row is None and label == "revenue":
            rev_row = row
        elif promo_row is None and label.startswith("promotional credit"):
            promo_row = row
        elif state_tax_row is None and label.startswith("state tax due"):
            state_tax_row = row
        elif lsa_row is None and label.startswith("local share assessment"):
            lsa_row = row

    if rev_row is None and promo_row is None and state_tax_row is None and lsa_row is None:
        return {}

    out: dict[str, PaFiscalTuple] = {}
    for column, month_label in col_to_label.items():
        gross_rev = _to_float(_cell(rev_row, column))
        promo = _to_float(_cell(promo_row, column))
        tax = sum_optional(_to_float(_cell(state_tax_row, column)), _to_float(_cell(lsa_row, column)))
        if gross_rev is not None or promo is not None or tax is not None:
            out[month_label] = (gross_rev, promo, tax)
    return out


def parse_sheet_fiscal(
    rows: list[tuple[object, ...]],
    col_to_label: dict[int, str],
) -> dict[str, PaFiscalTuple]:
    """Extract statewide ``{month_label: (gross_revenue, promo_deductions, tax)}`` from PGCB rows."""
    if not col_to_label:
        return {}
    for index, row in enumerate(rows):
        if clean_lower(row[0] if row else "") == "grand total":
            fiscal = _extract_fiscal_block(rows, index, col_to_label)
            if fiscal:
                return fiscal
    return {}


def parse_detail_fiscal(
    rows: list[tuple[object, ...]],
    col_to_label: dict[int, str],
    *,
    _op_blocks: list[tuple[int, str]] | None = None,
) -> dict[tuple[str, str], PaFiscalTuple]:
    """Extract per-(month_label, operator) fiscal metrics ``(gross_revenue, promo_deductions, tax)``."""
    if not col_to_label:
        return {}
    fiscal_by_op: dict[tuple[str, str], PaFiscalTuple] = {}
    for index, label in (_op_blocks if _op_blocks is not None else _iter_operator_blocks(rows)):
        block_fiscal = _extract_fiscal_block(rows, index, col_to_label)
        for month_label, vals in block_fiscal.items():
            fiscal_by_op[(month_label, label)] = vals
    return fiscal_by_op


def _parse_fy_xlsx_core(
    data: bytes,
    fy_start: int,
    fy_end: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[dict[str, PaMonthCore], dict[str, PaFiscalTuple], dict[tuple[str, str], PaFiscalTuple]]:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Pennsylvania workbooks")
    with open_xlsx_workbook(
        data,
        openpyxl_mod=openpyxl,
        log=log,
        error_prefix=f"could not open workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}",
    ) as workbook:
        if workbook is None or not workbook.sheetnames:
            return {}, {}, {}
        first_sheet_rows = [tuple(row) for row in workbook[workbook.sheetnames[0]].iter_rows(values_only=True)]
        col_to_label = parse_month_columns(first_sheet_rows, fy_start, fy_end)
        total_metrics: dict[str, tuple[float, float]] = {}
        sheet_fiscal: dict[str, PaFiscalTuple] = {}
        if col_to_label:
            for index, row in enumerate(first_sheet_rows):
                if clean_lower(row[0] if row else "") != "grand total":
                    continue
                if not total_metrics:
                    total_metrics = extract_section_metrics(
                        first_sheet_rows, index, col_to_label, ("revenue", "gross revenue (taxable)"),
                    )
                if not sheet_fiscal:
                    sheet_fiscal = _extract_fiscal_block(first_sheet_rows, index, col_to_label)
                if total_metrics and sheet_fiscal:
                    break
        retail_metrics, mobile_metrics = (
            parse_section_totals(
                first_sheet_rows,
                fy_start,
                fy_end,
                section_name,
                ("gross revenue (taxable)", "revenue"),
                col_to_label=col_to_label,
            )
            for section_name in ("Retail Sports Wagering", "Online Sports Wagering")
        )
        if not retail_metrics and not mobile_metrics:
            for sheet in workbook.worksheets[1:]:
                section = classify_sheet(sheet.title)
                if section is None:
                    continue
                parsed = parse_sheet_totals(
                    [tuple(row) for row in sheet.iter_rows(values_only=True)], fy_start, fy_end,
                )
                (retail_metrics if section == "retail" else mobile_metrics).update(parsed)
        op_blocks = list(_iter_operator_blocks(first_sheet_rows))
        detail_metrics = parse_detail_metrics(
            first_sheet_rows, fy_start, fy_end, col_to_label=col_to_label, _op_blocks=op_blocks,
        )
        detail_retail_metrics, detail_mobile_metrics = aggregate_detail_totals(detail_metrics)
        if detail_retail_metrics:
            retail_metrics.update(detail_retail_metrics)
        if detail_mobile_metrics:
            mobile_metrics.update(detail_mobile_metrics)
        detail_fiscal = parse_detail_fiscal(first_sheet_rows, col_to_label, _op_blocks=op_blocks)
        results: dict[str, PaMonthCore] = {}
        for label, (handle, revenue) in total_metrics.items():
            retail = retail_metrics.get(label)
            mobile = mobile_metrics.get(label)
            results[label] = (
                handle,
                revenue,
                retail[0] if retail else None,
                retail[1] if retail else None,
                mobile[0] if mobile else None,
                mobile[1] if mobile else None,
                detail_metrics.get(label, []),
            )
        return results, sheet_fiscal, detail_fiscal


def parse_local_file(local_dir: str, fy_start: int, fy_end: int) -> bytes | None:
    fy_token = f"fy{fy_start % 100:02d}-{fy_end % 100:02d}"
    matched = find_local_file(
        local_dir,
        lambda entry: entry.lower().endswith(".xlsx") and fy_token in entry.lower(),
    )
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
    """Fetch and parse Pennsylvania monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Pennsylvania sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    months = [(mdate.month, mdate.year, f"{MONTH_ABBR[mdate.month]} {mdate.year}") for mdate in target_dates]
    all_data: dict[str, PaMonthCore] = {}
    all_sheet_fiscal: dict[str, PaFiscalTuple] = {}
    all_detail_fiscal: dict[tuple[str, str], PaFiscalTuple] = {}
    anchors: list[dict[str, str]] | None = None

    needed_fys = sorted({fiscal_year_july(year, month) for month, year, _ in months})

    def _ensure_anchors() -> list[dict[str, str]]:
        nonlocal anchors
        if anchors is None:
            anchors = crawl_revenue_index(report=report, log=logger, target_fys=needed_fys)
            logger(f"INFO crawled {len(anchors)} PGCB anchors from {REVENUE_PAGE}")
        return anchors

    if report is not None and not offline:
        _ensure_anchors()

    for fy_start, fy_end in needed_fys:
        try:
            data = parse_local_file(local_dir, fy_start, fy_end) if local_dir else None
            if data:
                logger(f"INFO loaded local FY workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}")
            path: str | None = None
            was_cached = False
            fetch_called = [False]

            def _download_fy(fs: int = fy_start, fe: int = fy_end, called: list[bool] = fetch_called) -> bytes | None:
                called[0] = True
                return download_fy_workbook(fs, fe, anchors=_ensure_anchors(), report=report, log=logger)

            if data is None:
                path = cache_path(cache_dir, f"pennsylvania_fy_{fy_start}_{fy_end}.xlsx")
                was_cached = bool(path and os.path.isfile(path))
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=_download_fy,
                    log=logger,
                    cache_hit_message=f"INFO using cached FY workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}",
                    fetch_error_message=f"could not download Pennsylvania FY workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}",
                    validate=is_xlsx_bytes,
                    cache_ttl_seconds=recent_fy_cache_ttl(fy_end),
                    offline=offline,
                )
            if data is None:
                logger(f"WARN could not find FY workbook for {fy_start}-{fy_end}")
                continue
            fy_parsed, fy_sheet_fiscal, fy_detail_fiscal = _parse_fy_xlsx_core(
                data, fy_start, fy_end, log=logger,
            )
            fy_labels = [label for month, year, label in months if fiscal_year_july(year, month) == (fy_start, fy_end)]
            if (
                was_cached
                and not fetch_called[0]
                and not offline
                and path is not None
                and any(lbl not in fy_parsed or fy_parsed[lbl][0] <= 0 for lbl in fy_labels)
            ):
                refreshed = refresh_cached_if_stale(
                    path,
                    data,
                    fetcher=_download_fy,
                    validate=is_xlsx_bytes,
                    offline=offline,
                    log=logger,
                )
                if refreshed is not None:
                    fy_parsed, fy_sheet_fiscal, fy_detail_fiscal = _parse_fy_xlsx_core(
                        refreshed, fy_start, fy_end, log=logger,
                    )
            all_data.update(fy_parsed)
            all_sheet_fiscal.update(fy_sheet_fiscal)
            all_detail_fiscal.update(fy_detail_fiscal)
        except Exception as exc:
            logger(f"WARN failed Pennsylvania FY workbook processing for {fy_start}-{fy_end}: {exc}")

    results: list[StateMonthResult] = []
    for month, year, label in months:
        iso_month = format_iso_month(year, month)
        if label not in all_data:
            log_skip(logger, label, "no parsed value")
            continue
        handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, details = all_data[label]
        enriched_details = [
            (
                *op_tuple,
                *all_detail_fiscal.get((label, op_tuple[0]), (None, None, None)),
            )
            for op_tuple in details
        ]
        operator_details = build_operator_results(iso_month, enriched_details)
        s_gross, s_promo, s_tax = all_sheet_fiscal.get(label, (None, None, None))
        item = finalize_state_month(
            state=STATE_NAME,
            iso_month=iso_month,
            handle=handle,
            revenue=revenue,
            retail_handle=retail_handle,
            retail_revenue=retail_revenue,
            mobile_handle=mobile_handle,
            mobile_revenue=mobile_revenue,
            source_url=REVENUE_PAGE,
            details=operator_details,
            gross_revenue=s_gross,
            promo_deductions=s_promo,
            tax=s_tax,
            log=logger,
            label=label,
        )
        if item is None:
            continue
        results.append(item)
        log_month_result(logger, label, item)
    return results
