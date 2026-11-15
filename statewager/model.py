"""Data models, month windowing, validation, reconciliation, and logging."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import math
from typing import Callable, Iterable, Mapping, Sequence, TypeVar

from dateutil.relativedelta import relativedelta

T = TypeVar("T")

DQ_WARN_PREFIX = "DQ-WARN"

MONTH_NAMES: dict[int, str] = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}

MONTH_ABBR: dict[int, str] = {m: name[:3] for m, name in MONTH_NAMES.items()}

CORE_RESULT_FIELDS: tuple[str, ...] = (
    "state",
    "month",
    "handle",
    "revenue",
    "hold",
    "retail_handle",
    "retail_revenue",
    "retail_hold",
    "mobile_handle",
    "mobile_revenue",
    "mobile_hold",
    "source_url",
)

MONTH_NAME_TO_NUM: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


@dataclass
class OperatorMonthResult:
    """Operator- or property-level breakdown for a single state-month."""

    month: str
    operator: str
    handle: float | None
    revenue: float | None
    hold: float | None
    retail_handle: float | None = None
    retail_revenue: float | None = None
    mobile_handle: float | None = None
    mobile_revenue: float | None = None


@dataclass
class StateMonthResult:
    """Canonical monthly sports wagering metrics for a single state."""

    state: str
    month: str
    handle: float | None
    revenue: float | None
    hold: float | None
    retail_handle: float | None
    retail_revenue: float | None
    retail_hold: float | None
    mobile_handle: float | None
    mobile_revenue: float | None
    mobile_hold: float | None
    source_url: str | None
    extra: dict[str, float | None] = field(default_factory=dict)
    details: list[OperatorMonthResult] = field(default_factory=list)


def parse_month_spec(value: str | date | datetime | tuple[int, int]) -> date:
    """Normalize a month specification into a first-of-month ``date``.

    Accepts ``"YYYY-MM"``, ``"Mon YYYY"``, ``"Month YYYY"``, ``date``/``datetime``,
    or ``(year, month)`` tuples.
    """
    if isinstance(value, datetime):
        return date(value.year, value.month, 1)
    if isinstance(value, date):
        return date(value.year, value.month, 1)
    if isinstance(value, tuple) and len(value) == 2:
        year, month = int(value[0]), int(value[1])
        return date(year, month, 1)
    if isinstance(value, str):
        text = value.strip()
        if "-" in text:
            parts = text.split("-")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return date(int(parts[0]), int(parts[1]), 1)
        parts = text.split()
        if len(parts) == 2:
            mon = MONTH_NAME_TO_NUM.get(parts[0].lower())
            if mon is not None and parts[1].isdigit():
                return date(int(parts[1]), mon, 1)
    raise ValueError(f"Invalid month specification: {value!r} (expected 'YYYY-MM', 'Mon YYYY', date, or (year, month))")


def resolve_fetch_months(
    month_start: str | date | datetime | tuple[int, int],
    month_end: str | date | datetime | tuple[int, int] | None = None,
    *,
    launch_date: date | None = None,
) -> list[date]:
    """Return inclusive oldest-first ``date(YYYY, MM, 1)`` list for ``fetch()``."""
    start = parse_month_spec(month_start)
    if month_end is None:
        last_completed = date.today().replace(day=1) - relativedelta(months=1)
        end = max(start, last_completed)
    else:
        end = parse_month_spec(month_end)
        if end < start:
            raise ValueError(f"month_end ({end:%Y-%m}) must be on or after month_start ({start:%Y-%m})")
    if launch_date is not None:
        launch_month = launch_date.replace(day=1)
        if end < launch_month:
            return []
        start = max(start, launch_month)
    months: list[date] = []
    current = start
    while current <= end:
        months.append(current)
        current += relativedelta(months=1)
    return months


def format_iso_month(year: int, month: int) -> str:
    """Format ``(year, month)`` as canonical ISO ``YYYY-MM``."""
    return f"{year:04d}-{month:02d}"


def format_month_label(month_iso_or_date: str | date) -> str:
    """Format an ISO ``YYYY-MM`` string or ``date`` as ``'Mon YYYY'``."""
    d = parse_month_spec(month_iso_or_date)
    return d.strftime("%b %Y")


def label_to_iso_month(label: str) -> str:
    """Convert ``'Mon YYYY'`` or ``'Month YYYY'`` into ISO ``'YYYY-MM'``."""
    d = parse_month_spec(label)
    return format_iso_month(d.year, d.month)


def column_letter(index: int) -> str:
    """Convert a 1-based column index to A1 notation letters."""
    letters = []
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def build_state_logger(prefix: str) -> Callable[[str], None]:
    """Return a consistent logger wrapper used by the state modules."""
    logger = logging.getLogger(prefix)

    def _ensure_logging() -> None:
        if not logging.getLogger().handlers and not logger.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

    def log(message: str) -> None:
        _ensure_logging()
        text = f"{prefix} {message}".strip()
        if message.startswith("WARN "):
            logger.warning(text)
        elif message.startswith("ERROR "):
            logger.error(text)
        else:
            logger.info(text)

    return log


def format_summary(
    tabs_results: list[tuple[str, dict]],
    month_range_str: str,
    months_written: int,
) -> str:
    """Generate a consistent SUMMARY line for state runs.

    tabs_results: list of (tab_name, result_dict) pairs (main first, detail second)
    month_range_str: e.g. "Jan 2026 – Mar 2026"
    months_written: number of month rows in the new data payload
    """
    updated = [(t, r) for t, r in tabs_results if not r.get("skipped")]
    skipped = [(t, r) for t, r in tabs_results if r.get("skipped")]

    if not updated:
        tabs_str = " + ".join(t for t, _ in tabs_results)
        return f"SUMMARY no new data | {tabs_str} | {month_range_str}"

    parts = []
    for tab, result in updated:
        rows = result.get("rows_written", 0)
        parts.append(f"{tab} ({rows} rows)" if rows else tab)

    updated_str = " | ".join(parts)
    skipped_str = f" | unchanged: {', '.join(t for t, _ in skipped)}" if skipped else ""
    return f"SUMMARY updated | {updated_str} | {month_range_str} | {months_written} months written{skipped_str}"


def month_starts(
    n_months: int,
    *,
    launch_date: date | None = None,
    include_current_month: bool = False,
) -> list[date]:
    """Return oldest-first month starts for the requested window."""
    if n_months < 1:
        raise ValueError("n_months must be >= 1")
    anchor = date.today().replace(day=1)
    latest = anchor if include_current_month else anchor - relativedelta(months=1)
    if launch_date is not None:
        launch_month = launch_date.replace(day=1)
        if latest < launch_month:
            return []
    start = latest - relativedelta(months=n_months - 1)
    if launch_date is not None:
        start = max(start, launch_date.replace(day=1))
    months: list[date] = []
    current = start
    while current <= latest:
        months.append(current)
        current += relativedelta(months=1)
    return months


def month_range(
    n_months: int,
    min_months: int = 1,
    *,
    launch_date: date | None = None,
    include_current_month: bool = False,
    transform: Callable[[date], T] | None = None,
) -> list[tuple[int, int] | T]:
    """Return oldest-first trailing months as ``(year, month)`` tuples or transformed values."""
    if min_months < 1:
        raise ValueError("min_months must be >= 1")
    months = month_starts(
        max(n_months, min_months),
        launch_date=launch_date,
        include_current_month=include_current_month,
    )
    if transform is None:
        return [(month.year, month.month) for month in months]
    return [transform(month) for month in months]


def safe_float(value: object) -> float | None:
    """Coerce int/float (excluding bool/NaN/Inf) to finite float with +0.0 normalization."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    val = float(value)
    if not math.isfinite(val):
        return None
    return 0.0 if val == 0.0 else val


def fiscal_year_july(year: int, month: int) -> tuple[int, int]:
    """Return the July–June fiscal year ``(fy_start, fy_end)`` containing ``(year, month)``."""
    return (year, year + 1) if month >= 7 else (year - 1, year)


def hold_pct(handle: float | None, revenue: float | None) -> float | None:
    """Return revenue/handle * 100 rounded to two decimals, or None when invalid."""
    h_val = safe_float(handle)
    r_val = safe_float(revenue)
    if h_val is None or r_val is None or h_val == 0:
        return None
    rounded = round(r_val / h_val * 100, 2)
    return 0.0 if rounded == 0.0 else rounded


def round_or_blank(value: float | int | None) -> int | str:
    """Round numeric values for tabular output, otherwise return an empty string."""
    val = safe_float(value)
    return round(val) if val is not None else ""


def blank_if_none(value: object) -> object:
    """Convert None to empty string."""
    return "" if value is None else value


def _emit_warn(log: Callable[[str], None] | None, message: str) -> None:
    if log is not None:
        log(f"WARN {message}")
    else:
        logging.getLogger(__name__).warning("%s", message)


def validate_result(
    label: str,
    handle: float | None,
    revenue: float | None,
    *,
    max_abs_hold: float = 0.50,
    log: Callable[[str], None] | None = None,
) -> tuple[float, float] | None:
    """Validate handle/revenue pair. Logs warning and returns None if invalid."""
    if (
        handle is None
        or isinstance(handle, bool)
        or not isinstance(handle, (int, float))
        or not math.isfinite(float(handle))
        or handle <= 0
    ):
        _emit_warn(log, f"{label}: invalid handle {handle}")
        return None
    if (
        revenue is None
        or isinstance(revenue, bool)
        or not isinstance(revenue, (int, float))
        or not math.isfinite(float(revenue))
    ):
        _emit_warn(log, f"{label}: missing revenue")
        return None
    h = float(handle)
    r = float(revenue)
    hold = r / h
    if abs(hold) > max_abs_hold:
        _emit_warn(log, f"{label}: suspicious hold % {hold * 100:.1f}%")
    return h, r


def log_skip(log: Callable[[str], None], label: str, reason: str) -> None:
    """Log a standardized skip warning."""
    log(f"WARN skipped {label}; {reason}")


def reconcile_components(
    label: str,
    total_handle: float,
    total_revenue: float,
    retail_handle: float | None,
    retail_revenue: float | None,
    mobile_handle: float | None,
    mobile_revenue: float | None,
    *,
    tolerance: float = 1.0,
    log: Callable[[str], None] | None = None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Fill missing component values from totals and warn on inconsistent sums."""
    if retail_handle is None and mobile_handle is not None:
        retail_handle = total_handle - mobile_handle
    if mobile_handle is None and retail_handle is not None:
        mobile_handle = total_handle - retail_handle
    if retail_revenue is None and mobile_revenue is not None:
        retail_revenue = total_revenue - mobile_revenue
    if mobile_revenue is None and retail_revenue is not None:
        mobile_revenue = total_revenue - retail_revenue
    if retail_handle is not None and mobile_handle is not None:
        handle_delta = (retail_handle + mobile_handle) - total_handle
        if abs(handle_delta) > tolerance:
            _emit_warn(log, f"{label}: retail/mobile handle mismatch vs total by {handle_delta:.2f}")
    if retail_revenue is not None and mobile_revenue is not None:
        revenue_delta = (retail_revenue + mobile_revenue) - total_revenue
        if abs(revenue_delta) > tolerance:
            _emit_warn(log, f"{label}: retail/mobile revenue mismatch vs total by {revenue_delta:.2f}")
    return retail_handle, retail_revenue, mobile_handle, mobile_revenue


def build_operator_results(
    iso_month: str,
    entries: Iterable[Mapping[str, object] | Sequence[object]],
) -> list[OperatorMonthResult]:
    """Convert raw operator rows or dicts into ``OperatorMonthResult`` records."""
    results: list[OperatorMonthResult] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            operator = str(entry["operator"])
            handle = entry.get("handle")
            revenue = entry.get("revenue")
            retail_handle = entry.get("retail_handle")
            retail_revenue = entry.get("retail_revenue")
            mobile_handle = entry.get("mobile_handle")
            mobile_revenue = entry.get("mobile_revenue")
        else:
            seq = tuple(entry)
            if len(seq) == 3:
                op_raw, handle, revenue = seq
                retail_handle = retail_revenue = mobile_handle = mobile_revenue = None
            elif len(seq) == 7:
                (
                    op_raw,
                    handle,
                    revenue,
                    retail_handle,
                    retail_revenue,
                    mobile_handle,
                    mobile_revenue,
                ) = seq
            else:
                raise ValueError(f"Expected 3- or 7-item operator entry, got {len(seq)}")
            operator = str(op_raw)
        h_val = safe_float(handle)
        r_val = safe_float(revenue)
        results.append(
            OperatorMonthResult(
                month=iso_month,
                operator=operator,
                handle=h_val,
                revenue=r_val,
                hold=hold_pct(h_val, r_val),
                retail_handle=safe_float(retail_handle),
                retail_revenue=safe_float(retail_revenue),
                mobile_handle=safe_float(mobile_handle),
                mobile_revenue=safe_float(mobile_revenue),
            )
        )
    return results


def finalize_state_month(
    *,
    state: str,
    iso_month: str,
    handle: float | None,
    revenue: float | None,
    retail_handle: float | None = None,
    retail_revenue: float | None = None,
    mobile_handle: float | None = None,
    mobile_revenue: float | None = None,
    source_url: str | None = None,
    reconcile_splits: bool = True,
    tolerance: float = 1.0,
    strict_split_pct: float | None = None,
    extra: dict[str, float | None] | None = None,
    details: list[OperatorMonthResult] | None = None,
    log: Callable[[str], None] | None = None,
    label: str | None = None,
) -> StateMonthResult | None:
    """Validate totals, optionally reconcile retail/mobile splits, compute holds, and build ``StateMonthResult``."""
    tag = label or f"{state} {iso_month}"
    validated = validate_result(tag, handle, revenue, log=log)
    if validated is None:
        return None
    total_h, total_r = validated
    if reconcile_splits:
        rh, rr, mh, mr = reconcile_components(
            tag,
            total_h,
            total_r,
            retail_handle,
            retail_revenue,
            mobile_handle,
            mobile_revenue,
            tolerance=tolerance,
            log=log,
        )
        if strict_split_pct is not None and rh is not None and mh is not None:
            handle_delta_pct = abs((rh + mh) - total_h) / total_h
            if handle_delta_pct > strict_split_pct:
                if log:
                    log(
                        f"WARN {tag}: retail/mobile handle reconciliation failed "
                        f"({handle_delta_pct * 100:.2f}% delta vs total); blanking splits"
                    )
                rh = rr = mh = mr = None
            elif rr is not None and mr is not None and total_r != 0:
                revenue_delta_pct = abs((rr + mr) - total_r) / abs(total_r)
                if revenue_delta_pct > strict_split_pct:
                    if log:
                        log(
                            f"WARN {tag}: retail/mobile revenue reconciliation failed "
                            f"({revenue_delta_pct * 100:.2f}% delta vs total); blanking splits"
                        )
                    rh = rr = mh = mr = None
    else:
        rh, rr, mh, mr = retail_handle, retail_revenue, mobile_handle, mobile_revenue
    return StateMonthResult(
        state=state,
        month=iso_month,
        handle=total_h,
        revenue=total_r,
        hold=hold_pct(total_h, total_r),
        retail_handle=rh,
        retail_revenue=rr,
        retail_hold=hold_pct(rh, rr) if rh else None,
        mobile_handle=mh,
        mobile_revenue=mr,
        mobile_hold=hold_pct(mh, mr) if mh else None,
        source_url=source_url,
        extra=extra if extra is not None else {},
        details=details if details is not None else [],
    )


def dq_warn(log: Callable[[str], None] | None, message: str) -> None:
    """Emit a data-quality warning prefixed with DQ-WARN."""
    if log:
        log(f"WARN {DQ_WARN_PREFIX} {message}")


def find_stale_months(
    per_state_tab_data: dict[str, set[str]],
    *,
    min_peers: int = 5,
) -> list[tuple[str, str, int]]:
    """Identify states missing months that most peers already have.

    ``per_state_tab_data`` maps state-name to the set of "Mon YYYY" labels
    present in that state's dataset. Returns a list of
    ``(state, month_label, peer_count)`` tuples sorted by month then state.
    """
    month_peers: dict[str, set[str]] = {}
    for state, months in per_state_tab_data.items():
        for label in months:
            month_peers.setdefault(label, set()).add(state)

    def _month_sort_key(label: str) -> tuple[int, int]:
        parts = label.split()
        if len(parts) != 2:
            return (9999, 0)
        tok = parts[0].lower()
        mon = MONTH_NAME_TO_NUM.get(tok, MONTH_NAME_TO_NUM.get(tok[:3], 0))
        try:
            return (int(parts[1]), mon)
        except ValueError:
            return (9999, 0)

    stale: list[tuple[str, str, int]] = []
    for label, peers in month_peers.items():
        if len(peers) < min_peers:
            continue
        for state in per_state_tab_data:
            if state not in peers:
                stale.append((state, label, len(peers)))
    stale.sort(key=lambda triple: (_month_sort_key(triple[1]), triple[0]))
    return stale
