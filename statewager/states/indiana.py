"""Indiana sports wagering regulatory fetcher and parser (IGC)."""
from __future__ import annotations

from datetime import date, datetime
import io
import os
from typing import Callable

try:
    import openpyxl
except ImportError:
    openpyxl = None

from ..cache import cache_path, load_cached_or_fetch
from ..discover import DiscoveryReport
from ..http import probe_url_variants
from ..model import (
    StateMonthResult,
    build_operator_results,
    build_state_logger,
    finalize_state_month,
    format_iso_month,
    log_skip,
    resolve_fetch_months,
    validate_result,
)
from ..parse import is_number

STATE_NAME = "Indiana"
LAUNCH_DATE: date | None = None
EXTRA_HEADERS: list[str] = []
HAS_DETAILS = True

URL_VARIANT_TEMPLATES = [
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}-{mm}-Revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}_{mm}_Revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}-{mm}-revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{mm}-{yyyy}-Revenue.xlsx",
    "https://www.in.gov/igc/files/reports/{yyyy}/{yyyy}{mm}_Revenue.xlsx",
]
_DEFAULT_LOG = build_state_logger("IN")


def normalize_sheet_title(title: str) -> str:
    return "".join(ch for ch in title.lower().strip() if ch.isalnum() or ch.isspace()).strip()


def parse_summary_totals(workbook) -> tuple[float, float] | None:
    candidate_sheets = [
        ws
        for ws in workbook.worksheets
        if "sw" in ws.title.lower() and "summary" in ws.title.lower()
    ] or workbook.worksheets
    for sheet in candidate_sheets:
        handle_col = None
        agr_col = None
        for row in sheet.iter_rows(values_only=True):
            row = tuple(row)
            if not any(cell is not None for cell in row):
                continue
            if handle_col is None or agr_col is None:
                for index, cell in enumerate(row):
                    text = str(cell or "").strip().lower()
                    if handle_col is None and text == "handle":
                        handle_col = index
                    if agr_col is None and "taxable agr" in text:
                        agr_col = index
                continue
            label = str(row[0] if row else "").strip().lower()
            if label != "total":
                continue
            handle = row[handle_col] if handle_col < len(row) else None
            revenue = row[agr_col] if agr_col < len(row) else None
            if is_number(handle) and is_number(revenue):
                return float(handle), float(revenue)
    return None


def _find_tax_summary_sheet(workbook):
    def is_location_totals_sheet(sheet) -> bool:
        normalized_title = normalize_sheet_title(sheet.title.strip())
        if "sw tax summary" in sheet.title.strip().lower() or normalized_title in {"sheet7", "7 sw tax summary"}:
            return True
        for row in sheet.iter_rows(min_row=1, max_row=6, values_only=True):
            for cell in row:
                if "summary of sports wagering tax" in str(cell or "").strip().lower():
                    return True
        return False

    return next((ws for ws in workbook.worksheets if is_location_totals_sheet(ws)), None)


def _parse_tax_summary_sheet(
    workbook,
) -> tuple[dict[str, tuple[float, float]], float | None, dict[str, float]]:
    """Parse Indiana Sheet7 (location handle/AGR totals and taxes) in a single pass."""
    sheet = _find_tax_summary_sheet(workbook)
    if sheet is None:
        return {}, None, {}
    locations: dict[str, tuple[float, float]] = {}
    loc_taxes: dict[str, float] = {}
    total_tax: float | None = None
    saw_rows = False
    handle_col: int | None = None
    agr_col: int | None = None
    tax_col: int | None = None
    for row in sheet.iter_rows(values_only=True):
        matched_header = False
        if handle_col is None or agr_col is None:
            h_idx: int | None = None
            a_idx: int | None = None
            for index, cell in enumerate(row):
                text = str(cell or "").strip().lower()
                if h_idx is None and text == "handle":
                    h_idx = index
                if a_idx is None and "taxable agr" in text:
                    a_idx = index
            if h_idx is not None and a_idx is not None:
                handle_col = h_idx
                agr_col = a_idx
                matched_header = True
        if tax_col is None:
            th_idx: int | None = None
            t_idx: int | None = None
            for index, cell in enumerate(row):
                if index == 0:
                    continue
                text = str(cell or "").strip().lower()
                if th_idx is None and text == "handle":
                    th_idx = index
                if t_idx is None and "tax" in text and "taxable" not in text:
                    t_idx = index
            if th_idx is not None and t_idx is not None:
                tax_col = t_idx
                matched_header = True
        if matched_header:
            continue

        label = str(row[0] if row else "").strip()
        if not label or label.startswith("*"):
            continue
        if label.upper() == "TOTAL":
            if saw_rows:
                if tax_col is not None and tax_col < len(row) and is_number(row[tax_col]):
                    total_tax = float(row[tax_col])
                break
            continue

        if handle_col is not None and agr_col is not None:
            handle = row[handle_col] if handle_col < len(row) else None
            revenue = row[agr_col] if agr_col < len(row) else None
        else:
            handle = row[1] if len(row) > 1 else None
            revenue = row[2] if len(row) > 2 else None
            if not (is_number(handle) and is_number(revenue)) and len(row) > 5:
                handle = row[3] if len(row) > 3 else None
                revenue = row[5] if len(row) > 5 else None
        if is_number(handle) and is_number(revenue):
            saw_rows = True
            locations[label] = (float(handle), float(revenue))
        if tax_col is not None and tax_col < len(row) and is_number(row[tax_col]):
            saw_rows = True
            loc_taxes[label] = float(row[tax_col])
    if total_tax is None and loc_taxes:
        total_tax = sum(loc_taxes.values())
    return locations, total_tax, loc_taxes


def parse_location_totals(workbook) -> dict[str, tuple[float, float]]:
    locations, _total_tax, _loc_taxes = _parse_tax_summary_sheet(workbook)
    return locations


def parse_tax_summary(workbook) -> tuple[float | None, dict[str, float]]:
    """Extract ``(total_tax, {location_name: tax})`` from Indiana Sheet7 (7 SW Tax Summary)."""
    _locations, total_tax, loc_taxes = _parse_tax_summary_sheet(workbook)
    return total_tax, loc_taxes


def is_retail_detail_label(label: str) -> bool:
    lower = label.strip().lower()
    return lower == "retail" or lower.startswith("wc ") or "winner" in lower


def allocate_adjustments(retail_revenue: float, mobile_revenue: float, adjustments: float) -> tuple[float, float]:
    total = retail_revenue + mobile_revenue
    if total == 0:
        return retail_revenue + adjustments, mobile_revenue
    retail_share = retail_revenue / total if retail_revenue else 0.0
    retail_adjustment = adjustments * retail_share
    return retail_revenue + retail_adjustment, mobile_revenue + (adjustments - retail_adjustment)


def _parse_detail_sheet_blocks(
    workbook,
) -> dict[str, tuple[float, float, list[tuple[str, float, float]], float]] | None:
    detail_sheet = next(
        (
            ws
            for ws in workbook.worksheets
            if "sw details" in ws.title.strip().lower()
            or normalize_sheet_title(ws.title.strip()) in {"sheet8", "8 sw details"}
        ),
        None,
    )
    if detail_sheet is None:
        return None
    blocks: dict[str, tuple[float, float, list[tuple[str, float, float]], float]] = {}
    rows = [tuple(r) for r in detail_sheet.iter_rows(values_only=True)]
    for row_index, row in enumerate(rows):
        values = list(row)
        for offset in (0, 5, 10):
            if offset + 3 >= len(values):
                continue
            header = str(values[offset] or "").strip()
            metric = str(values[offset + 1] or "").strip().lower()
            revenue_header = str(values[offset + 3] or "").strip().lower()
            if not header or metric != "handle" or revenue_header != "gross receipts":
                continue
            location_name = header
            block_retail_handle = 0.0
            block_retail_revenue = 0.0
            skins: list[tuple[str, float, float]] = []
            block_adjustments = 0.0
            for detail_row in rows[row_index + 1:]:
                label = str((detail_row[offset] if offset < len(detail_row) else "") or "").strip()
                if not label:
                    break
                lower = label.lower()
                next_metric = str((detail_row[offset + 1] if offset + 1 < len(detail_row) else "") or "").strip().lower()
                if next_metric == "handle":
                    break
                handle = detail_row[offset + 1] if offset + 1 < len(detail_row) else None
                revenue = detail_row[offset + 3] if offset + 3 < len(detail_row) else None
                if lower == "adjustments":
                    if is_number(revenue):
                        block_adjustments += float(revenue)
                    continue
                if lower == "taxable agr":
                    blocks[location_name] = (
                        block_retail_handle,
                        block_retail_revenue,
                        skins,
                        block_adjustments,
                    )
                    break
                h_val = float(handle) if is_number(handle) else 0.0
                r_val = float(revenue) if is_number(revenue) else 0.0
                if is_retail_detail_label(label):
                    block_retail_handle += h_val
                    block_retail_revenue += r_val
                else:
                    skins.append((label, h_val, r_val))
    return blocks


def _breakdown_from_blocks(
    blocks: dict[str, tuple[float, float, list[tuple[str, float, float]], float]],
) -> dict[str, tuple[float, float, float, float]]:
    breakdowns: dict[str, tuple[float, float, float, float]] = {}
    for location_name, (ret_h, ret_raw_r, skins, adjustments) in blocks.items():
        mob_h = sum(sh for _, sh, _ in skins)
        mob_raw_r = sum(sr for _, _, sr in skins)
        adj_ret_r, adj_mob_r = allocate_adjustments(ret_raw_r, mob_raw_r, adjustments)
        breakdowns[location_name] = (ret_h, adj_ret_r, mob_h, adj_mob_r)
    return breakdowns


def parse_detail_breakdown(workbook) -> dict[str, tuple[float, float, float, float]] | None:
    blocks = _parse_detail_sheet_blocks(workbook)
    if blocks is None:
        return None
    return _breakdown_from_blocks(blocks)


def _format_skin_operator_name(location_name: str, skin_label: str) -> str:
    low = skin_label.strip().lower()
    if "bet365" in low:
        return f"{location_name} - bet365"
    if "betrivers" in low:
        return f"{location_name} - BetRivers"
    if "fanatics" in low:
        return f"{location_name} - Fanatics"
    if "espnbet" in low or "espn bet" in low:
        return f"{location_name} - ESPN Bet"
    if "thescore" in low:
        return f"{location_name} - theScore Bet"
    return f"{location_name} - {skin_label.strip()}"


def _allocate_component_adjustments(raw_revenues: list[float], adjustments: float) -> list[float]:
    if not raw_revenues:
        return []
    total_raw = sum(raw_revenues)
    if total_raw == 0:
        allocated = list(raw_revenues)
        allocated[0] += adjustments
        return allocated
    allocated: list[float] = []
    used = 0.0
    for idx, rev in enumerate(raw_revenues):
        if idx == len(raw_revenues) - 1:
            adj = adjustments - used
        else:
            adj = adjustments * (rev / total_raw) if rev else 0.0
            used += adj
        allocated.append(rev + adj)
    return allocated


def _allocate_component_tax(adj_revenues: list[float], total_tax: float | None) -> list[float | None]:
    if total_tax is None or not adj_revenues:
        return [None] * len(adj_revenues)
    weights = [max(0.0, rev) for rev in adj_revenues]
    total_w = sum(weights)
    if total_w <= 0:
        return [None] * len(adj_revenues)
    last_pos = max(idx for idx, w in enumerate(weights) if w > 0)
    allocated: list[float | None] = []
    used = 0.0
    for idx, w in enumerate(weights):
        if w <= 0:
            allocated.append(0.0)
        elif idx == last_pos:
            allocated.append(round(total_tax - used, 2))
        else:
            share = round(total_tax * (w / total_w), 2)
            used += share
            allocated.append(share)
    return allocated


def _build_publish_detail_rows_with_fiscal(
    locations: dict[str, tuple[float, float]],
    blocks: dict[str, tuple[float, float, list[tuple[str, float, float]], float]],
    loc_taxes: dict[str, float],
) -> list[tuple[object, ...]]:
    publish_rows: list[tuple[object, ...]] = []
    for location_name, (loc_h, loc_r) in locations.items():
        loc_tax = loc_taxes.get(location_name)
        block = blocks.get(location_name)
        if block is None:
            publish_rows.append((location_name, loc_h, loc_r, None, None, None, None, None, None, loc_tax))
            continue
        ret_h, ret_raw_r, skins, adjustments = block
        loc_gross = ret_raw_r + sum(sr for _, _, sr in skins)
        active_skins = [(lbl, sh, sr) for lbl, sh, sr in skins if sh != 0 or sr != 0]
        if len(active_skins) < 2:
            mob_h = sum(sh for _, sh, _ in skins)
            mob_raw_r = sum(sr for _, _, sr in skins)
            adj_ret_r, adj_mob_r = allocate_adjustments(ret_raw_r, mob_raw_r, adjustments)
            publish_rows.append(
                (location_name, loc_h, loc_r, ret_h, adj_ret_r, mob_h, adj_mob_r, loc_gross, None, loc_tax)
            )
            continue
        has_retail = ret_h != 0 or ret_raw_r != 0
        raw_revs: list[float] = ([ret_raw_r] if has_retail else []) + [sr for _, _, sr in active_skins]
        adj_revs = _allocate_component_adjustments(raw_revs, adjustments)
        comp_taxes = _allocate_component_tax(adj_revs, loc_tax)
        pos = 0
        if has_retail:
            adj_ret_r = adj_revs[pos]
            ret_tax = comp_taxes[pos]
            pos += 1
            publish_rows.append(
                (f"{location_name} (Retail)", ret_h, adj_ret_r, ret_h, adj_ret_r, None, None, ret_raw_r, None, ret_tax)
            )
        for skin_label, skin_h, skin_raw_r in active_skins:
            adj_skin_r = adj_revs[pos]
            skin_tax = comp_taxes[pos]
            pos += 1
            publish_rows.append(
                (
                    _format_skin_operator_name(location_name, skin_label),
                    skin_h,
                    adj_skin_r,
                    None,
                    None,
                    skin_h,
                    adj_skin_r,
                    skin_raw_r,
                    None,
                    skin_tax,
                )
            )
    return publish_rows


def _build_publish_detail_rows(
    locations: dict[str, tuple[float, float]],
    blocks: dict[str, tuple[float, float, list[tuple[str, float, float]], float]],
) -> list[tuple[str, float, float, float | None, float | None, float | None, float | None]]:
    return [
        row[:7]  # type: ignore[misc]
        for row in _build_publish_detail_rows_with_fiscal(locations, blocks, {})
    ]


def _parse_workbook_with_skins(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
    *,
    include_fiscal: bool = False,
):
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to parse Indiana workbooks")
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        log(f"WARN could not open workbook: {exc}")
        return None
    try:
        totals = parse_summary_totals(workbook)
        if totals is None:
            log("WARN Indiana workbook missing statewide summary totals")
            return None
        blocks = _parse_detail_sheet_blocks(workbook)
        if not blocks:
            log("WARN Indiana workbook missing operator retail/mobile breakdown")
            return None
        locations, total_tax, loc_taxes = _parse_tax_summary_sheet(workbook)
        handle, revenue = totals
        breakdown = _breakdown_from_blocks(blocks)
        loc_gross = {
            loc: ret_raw_r + sum(sr for _, _, sr in skins)
            for loc, (_ret_h, ret_raw_r, skins, _adj) in blocks.items()
        }
        state_gross_revenue = sum(loc_gross.values()) if loc_gross else None
        retail_handle = sum(values[0] for values in breakdown.values())
        retail_revenue = sum(values[1] for values in breakdown.values())
        mobile_handle = handle - retail_handle
        mobile_revenue = revenue - retail_revenue
        detail_rows: list[tuple[str, float, float, float | None, float | None, float | None, float | None]] = []
        for location_name, (location_handle, location_revenue) in locations.items():
            split = breakdown.get(location_name)
            if split is None:
                detail_rows.append((location_name, location_handle, location_revenue, None, None, None, None))
                continue
            detail_rows.append(
                (
                    location_name,
                    location_handle,
                    location_revenue,
                    split[0],
                    split[1],
                    split[2],
                    split[3],
                )
            )
        if include_fiscal:
            publish_rows = _build_publish_detail_rows_with_fiscal(locations, blocks, loc_taxes)
            return (
                handle,
                revenue,
                retail_handle,
                retail_revenue,
                mobile_handle,
                mobile_revenue,
                detail_rows,
                publish_rows,
                state_gross_revenue,
                total_tax,
                loc_gross,
                loc_taxes,
            )
        publish_rows = _build_publish_detail_rows(locations, blocks)
        return handle, revenue, retail_handle, retail_revenue, mobile_handle, mobile_revenue, detail_rows, publish_rows
    finally:
        workbook.close()


def parse_state_totals(
    data: bytes,
    log: Callable[[str], None] = _DEFAULT_LOG,
) -> tuple[
    float,
    float,
    float,
    float,
    float,
    float,
    list[tuple[str, float, float, float | None, float | None, float | None, float | None]],
] | None:
    parsed = _parse_workbook_with_skins(data, log=log)
    if parsed is None:
        return None
    return parsed[:7]


def parse_local_file(local_dir: str, yyyy: str, mm: str) -> bytes | None:
    prefix = f"{yyyy}-{mm}-revenue"
    for entry in sorted(os.listdir(local_dir)):
        if entry.lower().endswith(".xlsx") and entry.lower().startswith(prefix.lower()):
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
    """Fetch and parse Indiana monthly sports wagering results."""
    if openpyxl is None:
        raise RuntimeError("openpyxl is required to fetch Indiana sports wagering data")
    logger = log or _DEFAULT_LOG
    target_dates = resolve_fetch_months(month_start, month_end, launch_date=LAUNCH_DATE)
    current_year = date.today().year
    results: list[StateMonthResult] = []
    for mdate in target_dates:
        yyyy = mdate.strftime("%Y")
        mm = mdate.strftime("%m")
        label = mdate.strftime("%b %Y")
        iso_month = format_iso_month(mdate.year, mdate.month)
        source_url = URL_VARIANT_TEMPLATES[0].format(yyyy=yyyy, mm=mm)
        matched: list[str] = []
        try:
            logger(f"INFO processing {label}")
            data = parse_local_file(local_dir, yyyy, mm) if local_dir else None
            if data:
                logger(f"INFO parsed local workbook for {label}")
            if data is None:
                path = cache_path(cache_dir, f"indiana_{yyyy}_{mm}.xlsx")
                data = load_cached_or_fetch(
                    cache_file=path,
                    fetcher=lambda y=yyyy, m=mm: probe_url_variants(
                        URL_VARIANT_TEMPLATES,
                        {"yyyy": y, "mm": m},
                        signature_check=b"PK",
                        wayback_timestamp=f"{y}{m}",
                        log=logger,
                        report=report,
                        report_key=f"{y}-{m}",
                        matched_url=matched,
                    ),
                    log=logger,
                    cache_hit_message=f"INFO using cached file for {label}",
                    fetch_error_message=f"could not download Indiana workbook for {label}",
                    cache_ttl_seconds=7 * 86400 if int(yyyy) >= current_year else None,
                    offline=offline,
                )
            if matched:
                source_url = matched[0]
            if data is None:
                log_skip(logger, label, "workbook unavailable")
                continue
            parsed = _parse_workbook_with_skins(data, log=logger, include_fiscal=True)
            if parsed is None:
                log_skip(logger, label, "could not parse statewide totals")
                continue
            (
                handle,
                revenue,
                retail_handle,
                retail_revenue,
                mobile_handle,
                mobile_revenue,
                locations,
                publish_rows,
                state_gross_revenue,
                state_tax,
                loc_gross,
                loc_taxes,
            ) = parsed
            valid_locations: list[tuple[object, ...]] = []
            for (
                location_name,
                location_handle,
                location_revenue,
                location_retail_handle,
                location_retail_revenue,
                location_mobile_handle,
                location_mobile_revenue,
            ) in locations:
                location_validated = validate_result(
                    f"{label} {location_name}",
                    location_handle,
                    location_revenue,
                    log=logger,
                )
                if location_validated is None:
                    logger(f"WARN skipped detail row for {label} {location_name}; invalid handle/revenue")
                    continue
                valid_h, valid_r = location_validated
                valid_locations.append(
                    (
                        location_name,
                        valid_h,
                        valid_r,
                        location_retail_handle,
                        location_retail_revenue,
                        location_mobile_handle,
                        location_mobile_revenue,
                        loc_gross.get(location_name),
                        None,
                        loc_taxes.get(location_name),
                    )
                )
            operator_details = build_operator_results(iso_month, valid_locations)
            valid_publish_rows = [
                row for row in publish_rows
                if row[1] != 0 or row[2] != 0
            ]
            publish_operator_details = build_operator_results(iso_month, valid_publish_rows)
            item = finalize_state_month(
                state=STATE_NAME,
                iso_month=iso_month,
                handle=handle,
                revenue=revenue,
                retail_handle=retail_handle,
                retail_revenue=retail_revenue,
                mobile_handle=mobile_handle,
                mobile_revenue=mobile_revenue,
                source_url=source_url,
                details=operator_details,
                publish_details=publish_operator_details,
                gross_revenue=state_gross_revenue,
                tax=state_tax,
                log=logger,
                label=label,
            )
            if item is None:
                continue
            results.append(item)
            logger(
                f"INFO {label}: handle={item.handle:,.0f} revenue={item.revenue:,.0f} "
                f"retail_handle={item.retail_handle:,.0f} mobile_handle={item.mobile_handle:,.0f} "
                f"hold={item.hold:.1f}%"
            )
        except Exception as exc:
            logger(f"WARN failed {label}: {exc}")
    return results
