"""Aggregator cross-checks and historical anomaly detection."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Callable

from .discover import extract_month_label
from .http import fetch_bytes, fetch_text
from .model import dq_warn
from .parse import _parse_money

_AGGREGATOR_CACHE: dict[str, dict[str, tuple[float | None, float | None]]] = {}

_STATE_ABBR = {
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "louisiana": "LA",
    "missouri": "MO",
    "mississippi": "MS",
    "nevada": "NV",
    "ohio": "OH",
    "pennsylvania": "PA",
}


def _normalize_aggregator_month(value: str) -> str | None:
    """Coerce strings like 'May 2026' / 'May-2026' / '2026-05' to 'May YYYY'."""
    cleaned = re.sub(r"\s+", " ", (value or "").strip())
    if not cleaned:
        return None
    for fmt in ("%B %Y", "%b %Y", "%b-%Y", "%B-%Y", "%Y-%m", "%m/%Y", "%m-%Y"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%b %Y")
        except ValueError:
            continue
    return extract_month_label(cleaned)


def query_aggregator_sportshandle(
    state: str,
    *,
    log: Callable[[str], None] | None = None,
) -> dict[str, tuple[float | None, float | None]]:
    """Best-effort scrape of sportshandle.com revenue-tracker for ``state``."""
    abbr = _STATE_ABBR.get(state.lower())
    if abbr is None:
        return {}
    url = "https://sportshandle.com/sports-betting-revenue/"
    try:
        html = fetch_text(url, retries=2, timeout=30)
    except Exception as exc:
        if log:
            log(f"WARN sportshandle fetch failed: {exc}")
        return {}
    if not html:
        return {}
    state_lower = state.lower()
    pattern = re.compile(
        r"(?is)<h[1-4][^>]*>[^<]*" + re.escape(state_lower) + r"[^<]*</h[1-4]>(.*?)(?=<h[1-4]|\Z)"
    )
    match = pattern.search(html.lower())
    if not match:
        return {}
    section = match.group(1)
    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", section)
    results: dict[str, tuple[float | None, float | None]] = {}
    for row in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
        if len(cells) < 3:
            continue
        label = _normalize_aggregator_month(cells[0])
        if not label:
            continue
        handle = _parse_money(cells[1])
        revenue = _parse_money(cells[2])
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
    try:
        data = fetch_bytes(pdf_url, retries=2, timeout=60)
    except Exception as exc:
        if log:
            log(f"WARN UNLV PDF fetch failed: {exc}")
        return {}
    if not data:
        return {}
    state_lower = state.lower()
    results: dict[str, tuple[float | None, float | None]] = {}
    try:
        import io as _io
        with pdfplumber.open(_io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                text = (page.extract_text() or "").lower()
                if state_lower not in text:
                    continue
                for table in page.extract_tables() or []:
                    for row in table:
                        if not row or row[0] is None:
                            continue
                        label = _normalize_aggregator_month(str(row[0]))
                        if not label:
                            continue
                        amounts: list[float] = []
                        for cell in row[1:]:
                            amount = _parse_money(str(cell or ""))
                            if amount is not None:
                                amounts.append(amount)
                        if not amounts:
                            continue
                        handle = amounts[0]
                        revenue = amounts[1] if len(amounts) > 1 else None
                        results[label] = (handle, revenue)
    except Exception as exc:
        if log:
            log(f"WARN UNLV PDF parse failed: {exc}")
        return {}
    return results


def _aggregator_lookup(
    state: str,
    label: str,
    log: Callable[[str], None] | None,
) -> tuple[float | None, float | None] | None:
    """Best-effort lookup of (handle, revenue) for a state/month from aggregators."""
    key = f"sportshandle:{state}"
    if key not in _AGGREGATOR_CACHE:
        _AGGREGATOR_CACHE[key] = query_aggregator_sportshandle(state, log=log)
    if label in _AGGREGATOR_CACHE[key]:
        return _AGGREGATOR_CACHE[key][label]
    key = f"unlv:{state}"
    if key not in _AGGREGATOR_CACHE:
        _AGGREGATOR_CACHE[key] = query_aggregator_unlv(state, log=log)
    return _AGGREGATOR_CACHE[key].get(label)


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
    if ref_handle and handle:
        delta_pct = abs(handle - ref_handle) / ref_handle * 100
        if delta_pct > threshold_pct:
            dq_warn(log, f"{state} {label}: handle ${handle:,.0f} differs from aggregator ${ref_handle:,.0f} by {delta_pct:.1f}%")
            diverged = True
    if ref_revenue and revenue:
        delta_pct = abs(revenue - ref_revenue) / abs(ref_revenue) * 100
        if delta_pct > threshold_pct:
            dq_warn(log, f"{state} {label}: revenue ${revenue:,.0f} differs from aggregator ${ref_revenue:,.0f} by {delta_pct:.1f}%")
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
    if len(history) < 3:
        return True
    handles = [h for h, _ in history if h]
    if len(handles) < 3:
        return True
    mean = sum(handles) / len(handles)
    variance = sum((h - mean) ** 2 for h in handles) / len(handles)
    stdev = variance ** 0.5
    flagged = False
    if stdev > 0:
        zscore = abs(handle - mean) / stdev
        if zscore > zscore_threshold:
            dq_warn(log, f"{state} {label}: handle ${handle:,.0f} is {zscore:.1f}σ from trailing mean ${mean:,.0f}")
            flagged = True
    last_handle = handles[-1]
    if last_handle:
        mom = abs(handle - last_handle) / last_handle
        if mom > mom_threshold:
            dq_warn(log, f"{state} {label}: handle ${handle:,.0f} differs from prior month ${last_handle:,.0f} by {mom * 100:.0f}%")
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
