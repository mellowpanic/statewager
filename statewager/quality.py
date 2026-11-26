"""Aggregator cross-checks and historical anomaly detection."""
from __future__ import annotations

from datetime import datetime
import html
import io
import re
import threading
from typing import Any, Callable, TypeVar

from .discover import extract_month_label
from .http import fetch_bytes, fetch_text
from .model import MONTH_ABBR, dq_warn, safe_float
from .parse import PDFPLUMBER_LOCK, collapse_ws, parse_dollar
from .states import STATE_ABBR_TO_SLUG

_T = TypeVar("_T")
_AGGREGATOR_CACHE: dict[str, dict[str, tuple[float | None, float | None]]] = {}
_SPORTSHANDLE_HTML_CACHE: dict[tuple[str, Any], str] = {}
_UNLV_PAGES_CACHE: dict[tuple[str, Any], list[tuple[str, list[Any]]]] = {}
_INFLIGHT_LOADS: dict[tuple[str, Any], threading.Event] = {}
_AGGREGATOR_LOCK = threading.Lock()
_TR_RE = re.compile(r"(?s)<tr[^>]*>(.*?)</tr>")
_TD_RE = re.compile(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _source_cache_key(url: str, fetcher: Any) -> tuple[str, Any] | None:
    """Build a mock-safe hashable cache key for a URL + fetcher callable."""
    try:
        key = (url, fetcher)
        hash(key)
        return key
    except TypeError:
        return None


def _cached_source_fetch(
    cache: dict[tuple[str, Any], _T],
    url: str,
    fetcher: Any,
    loader: Callable[[], _T | None],
) -> _T | None:
    cache_key = _source_cache_key(url, fetcher)
    if cache_key is None:
        return loader()

    inflight_owner = False
    event: threading.Event | None = None
    while True:
        with _AGGREGATOR_LOCK:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
            event = _INFLIGHT_LOADS.get(cache_key)
            if event is None:
                event = threading.Event()
                _INFLIGHT_LOADS[cache_key] = event
                inflight_owner = True
                break
        event.wait()

    try:
        loaded = loader()
        if loaded is None:
            return None
        with _AGGREGATOR_LOCK:
            return cache.setdefault(cache_key, loaded)
    finally:
        if inflight_owner:
            with _AGGREGATOR_LOCK:
                _INFLIGHT_LOADS.pop(cache_key, None)
            event.set()


_STATE_ABBR: dict[str, str] = {
    slug: abbr.upper() for abbr, slug in STATE_ABBR_TO_SLUG.items()
}

_STATE_SECTION_RES: dict[str, re.Pattern[str]] = {
    state_lower: re.compile(
        r"(?s)<h[1-4][^>]*>(?:(?!</h[1-4]>).)*?\b"
        + re.escape(state_lower)
        + r"\b(?:(?!</h[1-4]>).)*?</h[1-4]>(.*?)(?=<h[1-4]|\Z)"
    )
    for state_lower in _STATE_ABBR
}


def _normalize_aggregator_month(value: str) -> str | None:
    """Coerce strings like 'May 2026' / 'May-2026' / '2026-05' to 'May YYYY'."""
    cleaned = collapse_ws(value)
    if not cleaned:
        return None
    label = extract_month_label(cleaned)
    if label is not None:
        return label
    for fmt in ("%m/%Y", "%m-%Y", "%Y-%m"):
        try:
            d = datetime.strptime(cleaned, fmt)
            return f"{MONTH_ABBR[d.month]} {d.year}"
        except ValueError:
            continue
    return None


def query_aggregator_sportshandle(
    state: str,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, tuple[float | None, float | None]]:
    """Best-effort scrape of sportshandle.com revenue-tracker for ``state``."""
    state_lower = state.strip().lower()
    section_re = _STATE_SECTION_RES.get(state_lower)
    if section_re is None:
        return {}
    url = "https://sportshandle.com/sports-betting-revenue/"

    def _load_html() -> str | None:
        try:
            html = fetch_text(url, retries=2, timeout=30)
        except Exception as exc:
            if log:
                log(f"WARN sportshandle fetch failed: {exc}")
            return None
        return html.lower() if html else None

    html_lower = _cached_source_fetch(_SPORTSHANDLE_HTML_CACHE, url, fetch_text, _load_html)
    if html_lower is None:
        return {}
    match = section_re.search(html_lower)
    if not match:
        return {}
    section = match.group(1)
    rows = _TR_RE.findall(section)
    results: dict[str, tuple[float | None, float | None]] = {}
    for row in rows:
        cells = [collapse_ws(html.unescape(_HTML_TAG_RE.sub(" ", c))) for c in _TD_RE.findall(row)]
        if len(cells) < 3:
            continue
        label = _normalize_aggregator_month(cells[0])
        if not label:
            continue
        handle = parse_dollar(cells[1])
        revenue = parse_dollar(cells[2])
        if handle is None and revenue is None:
            continue
        results[label] = (handle, revenue)
    return results


def query_aggregator_unlv(
    state: str,
    *,
    log: Callable[[str], None] | None = None,
    pdf_url: str = "https://gaming.library.unlv.edu/reports/us_sports_betting.pdf",
) -> dict[str, tuple[float | None, float | None]]:
    """Best-effort extraction of UNLV CGR per-state monthly totals."""
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        if log:
            log("WARN pdfplumber not installed; UNLV cross-check unavailable")
        return {}

    def _load_pages() -> list[tuple[str, list[Any]]] | None:
        try:
            data = fetch_bytes(pdf_url, retries=2, timeout=60)
        except Exception as exc:
            if log:
                log(f"WARN UNLV PDF fetch failed: {exc}")
            return None
        if not data:
            return None
        try:
            extracted: list[tuple[str, list[Any]]] = []
            with PDFPLUMBER_LOCK, pdfplumber.open(io.BytesIO(data)) as pdf:
                for page in pdf.pages:
                    text = (page.extract_text() or "").lower()
                    tables = page.extract_tables() or []
                    extracted.append((text, tables))
            return extracted
        except Exception as exc:
            if log:
                log(f"WARN UNLV PDF parse failed: {exc}")
            return None

    cached_pages = _cached_source_fetch(_UNLV_PAGES_CACHE, pdf_url, fetch_bytes, _load_pages)
    if cached_pages is None:
        return {}
    state_lower = state.strip().lower()
    if not state_lower:
        return {}
    state_word_re = re.compile(rf"\b{re.escape(state_lower)}\b")
    results: dict[str, tuple[float | None, float | None]] = {}
    for text, tables in cached_pages:
        if not state_word_re.search(text):
            continue
        for table in tables:
            for row in table:
                if not row or row[0] is None:
                    continue
                label = _normalize_aggregator_month(str(row[0]))
                if not label:
                    continue
                amounts: list[float] = []
                for cell in row[1:]:
                    amount = parse_dollar(cell)
                    if amount is not None:
                        amounts.append(amount)
                if not amounts:
                    continue
                handle = amounts[0]
                revenue = amounts[1] if len(amounts) > 1 else None
                results[label] = (handle, revenue)
    return results


def _aggregator_lookup(
    state: str,
    label: str,
    log: Callable[[str], None] | None,
) -> tuple[float | None, float | None] | None:
    """Best-effort lookup of (handle, revenue) for a state/month from aggregators."""
    state_key = state.strip().lower()
    for prefix, query_fn in (
        ("sportshandle", query_aggregator_sportshandle),
        ("unlv", query_aggregator_unlv),
    ):
        cache_key = f"{prefix}:{state_key}"
        with _AGGREGATOR_LOCK:
            data = _AGGREGATOR_CACHE.get(cache_key)
        if data is None:
            fetched = query_fn(state, log=log)
            with _AGGREGATOR_LOCK:
                data = _AGGREGATOR_CACHE.setdefault(cache_key, fetched)
        if label in data:
            return data[label]
    return None


def validate_against_aggregators(
    state: str,
    label: str,
    handle: float,
    revenue: float,
    *,
    threshold_pct: float = 10.0,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Compare parsed values to aggregator values, emit DQ-WARN if divergent."""
    try:
        ref = _aggregator_lookup(state, label, log)
    except Exception as exc:
        if log:
            log(f"WARN aggregator lookup failed for {state} {label}: {exc}")
        return True
    if ref is None:
        return True
    ref_handle, ref_revenue = ref
    diverged = False
    for metric, actual, expected in (("handle", handle, ref_handle), ("revenue", revenue, ref_revenue)):
        if actual is None or expected is None:
            continue
        actual_f = safe_float(actual)
        expected_f = safe_float(expected)
        if actual_f is None:
            dq_warn(log, f"{state} {label}: non-finite {metric} {actual!r}")
            diverged = True
            continue
        if expected_f is None:
            continue
        if expected_f == 0:
            if actual_f != 0:
                dq_warn(log, f"{state} {label}: {metric} ${actual_f:,.0f} differs from aggregator $0")
                diverged = True
            continue
        delta_pct = abs(actual_f - expected_f) / abs(expected_f) * 100
        if delta_pct > threshold_pct:
            dq_warn(log, f"{state} {label}: {metric} ${actual_f:,.0f} differs from aggregator ${expected_f:,.0f} by {delta_pct:.1f}%")
            diverged = True
    return not diverged


def check_anomaly(
    state: str,
    label: str,
    handle: float,
    revenue: float,
    history: list[tuple[float, float]],
    *,
    zscore_threshold: float = 3.0,
    mom_threshold: float = 0.5,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Check parsed values against trailing baseline; emit DQ-WARN on outliers."""
    del revenue
    if handle is None:
        return True
    h_val = safe_float(handle)
    if h_val is None:
        dq_warn(log, f"{state} {label}: non-finite handle {handle!r}")
        return False
    if len(history) < 3:
        return True
    handles = [hf for h, _ in history if (hf := safe_float(h)) is not None]
    if len(handles) < 3:
        return True
    mean = sum(handles) / len(handles)
    variance = sum((h - mean) ** 2 for h in handles) / len(handles)
    stdev = variance ** 0.5
    flagged = False
    if stdev > 0:
        zscore = abs(h_val - mean) / stdev
        if zscore > zscore_threshold:
            dq_warn(log, f"{state} {label}: handle ${h_val:,.0f} is {zscore:.1f}σ from trailing mean ${mean:,.0f}")
            flagged = True
    last_handle = handles[-1]
    if last_handle != 0:
        mom = abs(h_val - last_handle) / abs(last_handle)
        if mom > mom_threshold:
            dq_warn(log, f"{state} {label}: handle ${h_val:,.0f} differs from prior month ${last_handle:,.0f} by {mom * 100:.0f}%")
            flagged = True
    elif h_val != 0:
        dq_warn(log, f"{state} {label}: handle ${h_val:,.0f} diverges from $0 prior month baseline")
        flagged = True
    return not flagged


def run_quality_checks(
    state: str,
    label: str,
    handle: float,
    revenue: float,
    history: list[tuple[float, float]],
    *,
    log: Callable[[str], None] | None = None,
    check_aggregator: bool = True,
    check_outlier: bool = True,
) -> None:
    """Run aggregator + anomaly checks, surfacing DQ-WARN on divergence."""
    if check_aggregator:
        try:
            validate_against_aggregators(state, label, handle, revenue, log=log)
        except Exception as exc:
            if log:
                log(f"WARN aggregator check raised for {state} {label}: {exc}")
    if check_outlier:
        try:
            check_anomaly(state, label, handle, revenue, history, log=log)
        except Exception as exc:
            if log:
                log(f"WARN anomaly check raised for {state} {label}: {exc}")
