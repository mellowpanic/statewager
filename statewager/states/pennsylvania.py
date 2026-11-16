"""Pennsylvania sports wagering regulatory fetcher and parser (PGCB)."""
from __future__ import annotations

from datetime import date, datetime
from html import unescape
import io
import os
import re
from typing import Callable, Iterator
from urllib.parse import urljoin

try:
    import openpyxl
except ImportError:
    openpyxl = None

from dateutil.relativedelta import relativedelta

from ..cache import cache_path, load_cached_or_fetch, refresh_cached_if_stale
from ..discover import (
    AnchorParser,
    DiscoveryReport,
    classify_sportsbook_filename,
    crawl_anchors,
)
from ..http import fetch_bytes, fetch_text, fetch_with_wayback_fallback
from ..model import (
    MONTH_ABBR,
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    fiscal_year_july,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
)
from ..parse import is_number

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
_FY_TEXT_RE = re.compile(r"fy[ _-]?(\d{2})[ _-]?(\d{2})")
_WS_RE = re.compile(r"\s+")

_DEFAULT_LOG = build_state_logger("PA")


def get_fy(cal_month: int, cal_year: int) -> tuple[int, int]:
    return fiscal_year_july(cal_year, cal_month)


def _absolute_url(href: str) -> str:
    if not href:
        return ""
    if href.startswith("http"):
        return href
    return urljoin(PGCB_BASE + "/", href.lstrip("/"))


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
    except Exception:
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
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)
            aggregated.append({
                "href": _absolute_url(href),
                "title": link.get("title", ""),
                "text": link.get("text", ""),
            })

    if index_html:
        parser = AnchorParser()
        parser.feed(index_html)
        _collect(parser.links, REVENUE_PAGE)
    else:
        _collect(crawl_anchors(REVENUE_PAGE, log=log), REVENUE_PAGE)
    for (_fy_start, _fy_end), filter_id in sorted(filter_ids.items(), reverse=True):
        url = f"{REVENUE_PAGE}?field_gaming_revenue_fiscal_year_target_id={filter_id}"
        _collect(crawl_anchors(url, log=log), url)
    return aggregated


def _classify_pa_anchor(
    href: str, title: str, text: str,
) -> tuple[str, tuple[int, int] | None]:
    lower_href = href.lower()
    if not lower_href.endswith(".xlsx") and not lower_href.endswith(".pdf"):
        return "other", None
    combined = " ".join(part for part in (href, title, text) if part).lower()
    fy_match = _FY_TEXT_RE.search(combined)
    fy_pair: tuple[int, int] | None = None
    if fy_match:
        fy_pair = (2000 + int(fy_match.group(1)), 2000 + int(fy_match.group(2)))
    if "sports wagering" in combined and ("monthly" in combined or "summary" in combined) and fy_pair:
        return "fy-summary", fy_pair
    classification = classify_sportsbook_filename(href, title=title, text=text)
    if classification is not None:
        return "monthly", fy_pair
    return "other", fy_pair


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

    for anchor in anchors:
        category, fy_pair = _classify_pa_anchor(anchor["href"], anchor["title"], anchor["text"])
        combined = " ".join((anchor["href"], anchor["title"], anchor["text"])).lower()
        if category == "fy-summary" and (fy_pair == (fy_start, fy_end) or fy_token_lower in combined):
            return anchor["href"]

    for anchor in anchors:
        lower_href = anchor["href"].lower()
        if lower_href.endswith(".xlsx") and fy_token_lower in lower_href and "sport" in lower_href:
            return anchor["href"]

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
                if data and data.startswith(b"PK"):
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
        link, timestamp_yyyymm=timestamp, signature_check=b"PK", log=log,
    )
    if data and data.startswith(b"PK"):
        log(f"INFO downloaded {link}")
        if report is not None:
            report.add_match(f"FY{fy_start % 100:02d}-{fy_end % 100:02d}", link)
        return data
    if data:
        log(f"WARN FY{fy_start % 100:02d}-{fy_end % 100:02d} URL did not return XLSX content: {link}")
    return None


def normalize_label(value: object) -> str:
    return _WS_RE.sub(" ", str(value or "").strip()).lower()


def extract_section_metrics(
    rows: list[tuple[object, ...]],
    start_index: int,
    col_to_label: dict[int, str],
    revenue_labels: tuple[str, ...],
) -> dict[str, tuple[float, float]]:
    handle_row = None
    revenue_row = None
    for row in rows[start_index + 1:start_index + 8]:
        label = normalize_label(row[0] if row else "")
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
    (month_index, abbr, re.compile(rf"\b{abbr}[a-z]*\b", re.IGNORECASE))
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
        if not text:
            continue
        for month_index, month, pattern in _MONTH_HEADER_PATTERNS:
            if pattern.search(text):
                year = fy_end if month_index <= 6 else fy_start
                col_to_label[column] = f"{month} {year}"
                break
    return col_to_label


def parse_sheet_totals(
    rows: list[tuple[object, ...]],
    fy_start: int,
    fy_end: int,
    *,
    col_to_label: dict[int, str] | None = None,
) -> dict[str, tuple[float, float]]:
    if col_to_label is None:
        col_to_label = parse_month_columns(rows, fy_start, fy_end)
    if not col_to_label:
        return {}
    for index, row in enumerate(rows):
        if normalize_label(row[0] if row else "") == "grand total":
            results = extract_section_metrics(
                rows,
                index,
                col_to_label,
                ("revenue", "gross revenue (taxable)"),
            )
            if results:
                return results
    return {}


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
    target = normalize_label(section_label)
    for index, row in enumerate(rows):
        if normalize_label(row[0] if row else "") == target:
            return extract_section_metrics(rows, index, col_to_label, revenue_labels)
    return {}


def classify_sheet(title: str) -> str | None:
    lower = title.lower()
    if "retail" in lower:
        return "retail"
    if "online" in lower or "mobile" in lower:
        return "mobile"
    return None


def _to_float(value: object) -> float | None:
    return float(value) if is_number(value) else None


_SKIP_BLOCK_LABELS: frozenset[str] = frozenset({
    "GRAND TOTAL",
    "TOTAL SPORTS WAGERING",
    "RETAIL SPORTS WAGERING",
    "ONLINE SPORTS WAGERING",
})


def _iter_operator_blocks(rows: list[tuple[object, ...]]) -> Iterator[tuple[int, str]]:
    """Yield ``(start_index, operator_label)`` for each 16-row PGCB operator block."""
    index = 0
    while index < len(rows):
        label = str(rows[index][0] if rows[index] else "").strip()
        if not label or label.upper() in _SKIP_BLOCK_LABELS:
            index += 1
            continue
        if index + 15 >= len(rows):
            break
        if (
            normalize_label(rows[index + 1][0] if rows[index + 1] else "") != "total sports wagering"
            or normalize_label(rows[index + 2][0] if rows[index + 2] else "") != "handle*"
            or normalize_label(rows[index + 3][0] if rows[index + 3] else "") != "revenue"
        ):
            index += 1
            continue
        yield index, label
        index += 16


def parse_detail_metrics(
    rows: list[tuple[object, ...]],
    fy_start: int,
    fy_end: int,
    *,
    col_to_label: dict[int, str] | None = None,
) -> dict[str, list[tuple[str, float, float, float | None, float | None, float | None, float | None]]]:
    if col_to_label is None:
        col_to_label = parse_month_columns(rows, fy_start, fy_end)
    if not col_to_label:
        return {}
    details: dict[str, list[tuple[str, float, float, float | None, float | None, float | None, float | None]]] = {
        label: [] for label in col_to_label.values()
    }
    for index, label in _iter_operator_blocks(rows):
        retail_section = normalize_label(rows[index + 8][0] if rows[index + 8] else "")
        retail_metric = normalize_label(rows[index + 9][0] if rows[index + 9] else "")
        retail_revenue_metric = normalize_label(rows[index + 10][0] if rows[index + 10] else "")
        mobile_section = normalize_label(rows[index + 13][0] if rows[index + 13] else "")
        mobile_metric = normalize_label(rows[index + 14][0] if rows[index + 14] else "")
        mobile_revenue_metric = normalize_label(rows[index + 15][0] if rows[index + 15] else "")
        handle_row = rows[index + 2]
        revenue_row = rows[index + 3]
        retail_handle_row = rows[index + 9] if retail_section == "retail sports wagering" and retail_metric == "handle*" else None
        retail_revenue_row = rows[index + 10] if retail_revenue_metric in {"gross revenue (taxable)", "revenue"} else None
        mobile_handle_row = rows[index + 14] if mobile_section == "online sports wagering" and mobile_metric == "handle*" else None
        mobile_revenue_row = rows[index + 15] if mobile_revenue_metric in {"gross revenue (taxable)", "revenue"} else None
        for column, month_label in col_to_label.items():
            handle = handle_row[column] if column < len(handle_row) else None
            revenue = revenue_row[column] if column < len(revenue_row) else None
            if is_number(handle) and is_number(revenue) and (handle != 0 or revenue != 0):
                retail_handle = retail_handle_row[column] if retail_handle_row and column < len(retail_handle_row) else None
                retail_revenue = retail_revenue_row[column] if retail_revenue_row and column < len(retail_revenue_row) else None
                mobile_handle = mobile_handle_row[column] if mobile_handle_row and column < len(mobile_handle_row) else None
                mobile_revenue = mobile_revenue_row[column] if mobile_revenue_row and column < len(mobile_revenue_row) else None
                details.setdefault(month_label, []).append(
                    (
                        label,
                        float(handle),
                        float(revenue),
                        _to_float(retail_handle),
                        _to_float(retail_revenue),
                        _to_float(mobile_handle),
                        _to_float(mobile_revenue),
                    )
                )
    return details


def aggregate_detail_totals(
    details: dict[str, list[tuple[str, float, float, float | None, float | None, float | None, float | None]]],
) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float]]]:
    retail_metrics: dict[str, tuple[float, float]] = {}
    mobile_metrics: dict[str, tuple[float, float]] = {}
    for month_label, month_details in details.items():
        retail_handle = 0.0
        retail_revenue = 0.0
        retail_found = False
        mobile_handle = 0.0
        mobile_revenue = 0.0
        mobile_found = False
        for (
            _,
            _,
            _,
            operator_retail_handle,
            operator_retail_revenue,
            operator_mobile_handle,
            operator_mobile_revenue,
        ) in month_details:
            if operator_retail_handle is not None and operator_retail_revenue is not None:
                retail_handle += operator_retail_handle
                retail_revenue += operator_retail_revenue
                retail_found = True
            if operator_mobile_handle is not None and operator_mobile_revenue is not None:
                mobile_handle += operator_mobile_handle
                mobile_revenue += operator_mobile_revenue
                mobile_found = True
        if retail_found:
            retail_metrics[month_label] = (retail_handle, retail_revenue)
        if mobile_found:
            mobile_metrics[month_label] = (mobile_handle, mobile_revenue)
    return retail_metrics, mobile_metrics


def _extract_fiscal_block(
    rows: list[tuple[object, ...]],
    start_index: int,
    col_to_label: dict[int, str],
) -> dict[str, tuple[float | None, float | None, float | None]]:
    """Extract ``{month_label: (gross_revenue, promo_deductions, tax)}`` from a PA summary/operator block."""
    rev_row = None
    promo_row = None
    state_tax_row = None
    lsa_row = None
    for row in rows[start_index + 1:start_index + 10]:
        label = normalize_label(row[0] if row else "")
        if not label:
            continue
        if label in ("retail sports wagering", "online sports wagering"):
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

    out: dict[str, tuple[float | None, float | None, float | None]] = {}
    for column, month_label in col_to_label.items():
        gross_rev = _to_float(rev_row[column]) if rev_row and column < len(rev_row) else None
        promo = _to_float(promo_row[column]) if promo_row and column < len(promo_row) else None
        st = _to_float(state_tax_row[column]) if state_tax_row and column < len(state_tax_row) else None
        lsa = _to_float(lsa_row[column]) if lsa_row and column < len(lsa_row) else None
        tax = (st or 0.0) + (lsa or 0.0) if (st is not None or lsa is not None) else None
        if gross_rev is not None or promo is not None or tax is not None:
            out[month_label] = (gross_rev, promo, tax)
    return out


def parse_sheet_fiscal(
    rows: list[tuple[object, ...]],
    col_to_label: dict[int, str],
) -> dict[str, tuple[float | None, float | None, float | None]]:
    """Extract statewide ``{month_label: (gross_revenue, promo_deductions, tax)}`` from PGCB rows."""
    if not col_to_label:
        return {}
    for index, row in enumerate(rows):
        if normalize_label(row[0] if row else "") == "grand total":
            fiscal = _extract_fiscal_block(rows, index, col_to_label)
            if fiscal:
                return fiscal
    return {}


def parse_detail_fiscal(
    rows: list[tuple[object, ...]],
    col_to_label: dict[int, str],
) -> dict[tuple[str, str], tuple[float | None, float | None, float | None]]:
    """Extract per-(month_label, operator) fiscal metrics ``(gross_revenue, promo_deductions, tax)``."""
    if not col_to_label:
        return {}
    fiscal_by_op: dict[tuple[str, str], tuple[float | None, float | None, float | None]] = {}
    for index, label in _iter_operator_blocks(rows):
        block_fiscal = _extract_fiscal_block(rows, index, col_to_label)
        for month_label, vals in block_fiscal.items():
            fiscal_by_op[(month_label, label)] = vals
    return fiscal_by_op


def _parse_fy_xlsx_core(
    data: bytes,
    fy_start: int,
    fy_end: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    dict[
        str,
        tuple[
            float,
            float,
            float | None,
            float | None,
            float | None,
            float | None,
            list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
        ],
    ],
    dict[str, tuple[float | None, float | None, float | None]],
    dict[tuple[str, str], tuple[float | None, float | None, float | None]],
]:
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Pennsylvania workbooks")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}: {exc}")
        return {}, {}, {}
    try:
        first_sheet_rows = [tuple(row) for row in workbook[workbook.sheetnames[0]].iter_rows(values_only=True)]
        col_to_label = parse_month_columns(first_sheet_rows, fy_start, fy_end)
        total_metrics = parse_sheet_totals(first_sheet_rows, fy_start, fy_end, col_to_label=col_to_label)
        retail_metrics = parse_section_totals(
            first_sheet_rows,
            fy_start,
            fy_end,
            "Retail Sports Wagering",
            ("gross revenue (taxable)", "revenue"),
            col_to_label=col_to_label,
        )
        mobile_metrics = parse_section_totals(
            first_sheet_rows,
            fy_start,
            fy_end,
            "Online Sports Wagering",
            ("gross revenue (taxable)", "revenue"),
            col_to_label=col_to_label,
        )
        if not retail_metrics and not mobile_metrics:
            retail_metrics = {}
            mobile_metrics = {}
            for sheet in workbook.worksheets[1:]:
                section = classify_sheet(sheet.title)
                if section is None:
                    continue
                parsed = parse_sheet_totals(
                    [tuple(row) for row in sheet.iter_rows(values_only=True)], fy_start, fy_end,
                )
                if section == "retail":
                    retail_metrics.update(parsed)
                else:
                    mobile_metrics.update(parsed)
        detail_metrics = parse_detail_metrics(first_sheet_rows, fy_start, fy_end, col_to_label=col_to_label)
        detail_retail_metrics, detail_mobile_metrics = aggregate_detail_totals(detail_metrics)
        if detail_retail_metrics:
            retail_metrics = detail_retail_metrics
        if detail_mobile_metrics:
            mobile_metrics = detail_mobile_metrics
        sheet_fiscal = parse_sheet_fiscal(first_sheet_rows, col_to_label)
        detail_fiscal = parse_detail_fiscal(first_sheet_rows, col_to_label)
        results: dict[
            str,
            tuple[
                float,
                float,
                float | None,
                float | None,
                float | None,
                float | None,
                list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
            ],
        ] = {}
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
    finally:
        workbook.close()


def parse_fy_xlsx(
    data: bytes,
    fy_start: int,
    fy_end: int,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> dict[
    str,
    tuple[
        float,
        float,
        float | None,
        float | None,
        float | None,
        float | None,
        list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
    ],
]:
    results, _, _ = _parse_fy_xlsx_core(data, fy_start, fy_end, log=log)
    return results


def parse_local_file(local_dir: str, fy_start: int, fy_end: int) -> bytes | None:
    fy_token = f"fy{fy_start % 100:02d}-{fy_end % 100:02d}"
    for entry in sorted(os.listdir(local_dir)):
        if entry.lower().endswith(".xlsx") and fy_token in entry.lower():
            with open(os.path.join(local_dir, entry), "rb") as file_handle:
                return file_handle.read()
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
    """Fetch and parse Pennsylvania monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Pennsylvania sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    months = [(mdate.month, mdate.year, mdate.strftime("%b %Y")) for mdate in target_dates]
    all_data: dict[
        str,
        tuple[
            float,
            float,
            float | None,
            float | None,
            float | None,
            float | None,
            list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
        ],
    ] = {}
    all_sheet_fiscal: dict[str, tuple[float | None, float | None, float | None]] = {}
    all_detail_fiscal: dict[tuple[str, str], tuple[float | None, float | None, float | None]] = {}
    anchors: list[dict[str, str]] | None = None

    needed_fys = sorted({get_fy(month, year) for month, year, _ in months})

    def _ensure_anchors() -> list[dict[str, str]]:
        nonlocal anchors
        if anchors is None:
            try:
                anchors = crawl_revenue_index(report=report, log=logger, target_fys=needed_fys)
            except TypeError as exc:
                if "target_fys" not in str(exc):
                    raise
                anchors = crawl_revenue_index(report=report, log=logger)
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
            if data is None:
                path = cache_path(cache_dir, f"pennsylvania_fy_{fy_start}_{fy_end}.xlsx")
                was_cached = bool(path and os.path.isfile(path))
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda fs=fy_start, fe=fy_end: download_fy_workbook(
                        fs, fe, anchors=_ensure_anchors(), report=report, log=logger,
                    ),
                    log=logger,
                    cache_hit_message=f"INFO using cached FY workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}",
                    fetch_error_message=f"could not download Pennsylvania FY workbook FY{fy_start % 100:02d}-{fy_end % 100:02d}",
                    validate=lambda payload: payload.startswith(b"PK"),
                    offline=offline,
                )
            if data is None:
                logger(f"WARN could not find FY workbook for {fy_start}-{fy_end}")
                continue
            fy_parsed, fy_sheet_fiscal, fy_detail_fiscal = _parse_fy_xlsx_core(
                data, fy_start, fy_end, log=logger,
            )
            fy_labels = [label for month, year, label in months if get_fy(month, year) == (fy_start, fy_end)]
            if (
                was_cached
                and not offline
                and path is not None
                and any(lbl not in fy_parsed or fy_parsed[lbl][0] <= 0 for lbl in fy_labels)
            ):
                refreshed = refresh_cached_if_stale(
                    path,
                    data,
                    fetcher=lambda fs=fy_start, fe=fy_end: download_fy_workbook(
                        fs, fe, anchors=_ensure_anchors(), report=report, log=logger,
                    ),
                    validate=lambda payload: payload.startswith(b"PK"),
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
        logger(
            f"INFO {label}: handle={item.handle:,.0f} revenue={item.revenue:,.0f} "
            f"retail_handle={item.retail_handle if item.retail_handle is not None else 'n/a'} "
            f"mobile_handle={item.mobile_handle if item.mobile_handle is not None else 'n/a'} "
            f"hold={(item.hold or 0.0):.1f}%"
        )
    return results
