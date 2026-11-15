"""HTTP client helpers with retry/backoff, Wayback fallback, and URL probing."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import random
import re
import threading
import time
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .parse import ensure_xlsx

if TYPE_CHECKING:
    from .discover import DiscoveryReport

_THREAD_LOCAL = threading.local()
_ORIG_REQUESTS_REQUEST = requests.request
_ORIG_REQUESTS_SESSION = requests.Session


def _get_session() -> requests.Session:
    """Return a thread-local pooled HTTP session."""
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _THREAD_LOCAL.session = session
    return session


def _resolve_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return max(0.0, retry_at.timestamp() - time.time())


def _sleep_with_jitter(base_seconds: float, *, jitter_max: float = 0.35) -> None:
    time.sleep(max(0.0, base_seconds) + random.uniform(0.0, jitter_max))


def retry_request(
    method: str,
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    data: bytes | str | dict[str, object] | None = None,
) -> requests.Response:
    """HTTP request with retry/backoff for transient failures."""
    if session is not None:
        client = session
    elif requests.request is _ORIG_REQUESTS_REQUEST and requests.Session is _ORIG_REQUESTS_SESSION:
        client = _get_session()
    else:
        client = requests
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = client.request(method, url, timeout=timeout, headers=headers, data=data)
            if response.status_code in {429, 503} and attempt < retries:
                retry_after = _resolve_retry_after(response.headers.get("Retry-After"))
                backoff = retry_after if retry_after is not None else max(1.0, attempt * 2.0)
                _sleep_with_jitter(backoff)
                continue
            if response.status_code == 404:
                return response
            if 500 <= response.status_code < 600 and attempt < retries:
                _sleep_with_jitter(float(attempt))
                continue
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < retries:
                _sleep_with_jitter(float(attempt))
    raise RuntimeError(f"Failed to {method.upper()} {url} after {retries} attempts") from last_exc


def retry_get(
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> requests.Response:
    """HTTP GET with retry loop. Returns response on success and raises on final failure."""
    return retry_request(
        "GET",
        url,
        retries=retries,
        timeout=timeout,
        headers=headers,
        session=session,
    )


def fetch_bytes(
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
) -> bytes | None:
    """HTTP GET returning bytes, or None on 404. Retries 429/5xx/timeouts."""
    response = retry_get(url, retries=retries, timeout=timeout, headers=headers, session=session)
    if response.status_code == 404:
        return None
    return response.content


def fetch_text(
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    encoding: str = "utf-8",
) -> str | None:
    """HTTP GET returning decoded text, or None on 404."""
    data = fetch_bytes(url, retries=retries, timeout=timeout, headers=headers, session=session)
    return data.decode(encoding, errors="replace") if data else None


_XLS_SIGNATURES = (b"PK", b"\xd0\xcf\x11\xe0")
_HTML_PREFIXES = (b"<!doctype html", b"<html", b"<head")
_NON_DIGIT_RE = re.compile(r"[^0-9]")
_WAYBACK_TS_RE = re.compile(r"\d{4,14}")


def _signature_from_extension(url: str) -> bytes | None:
    lower = urlparse(url).path.lower()
    if lower.endswith(".pdf"):
        return b"%PDF"
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return b"PK"
    return None


def _matches_signature(
    data: bytes | None,
    url: str,
    signature_check: bytes | None | str,
) -> bool:
    if not data:
        return False
    if signature_check == "auto":
        lower = urlparse(url).path.lower()
        if lower.endswith(".pdf"):
            return data.startswith(b"%PDF")
        if lower.endswith(".xlsx"):
            return data.startswith(b"PK")
        if lower.endswith(".xls"):
            return data.startswith(_XLS_SIGNATURES)
        if not lower.endswith((".html", ".htm")):
            head = data.lstrip()[:64].lower()
            if head.startswith(_HTML_PREFIXES):
                return False
        return True
    if isinstance(signature_check, (bytes, bytearray)):
        return data.startswith(bytes(signature_check))
    return True


def _should_try_wayback(timestamp_yyyymm: str | None) -> bool:
    """Return True unless ``timestamp_yyyymm`` is for the current or a future month."""
    if not timestamp_yyyymm:
        return True
    digits = _NON_DIGIT_RE.sub("", timestamp_yyyymm)
    if len(digits) < 6:
        return True
    try:
        year, month = int(digits[:4]), int(digits[4:6])
    except ValueError:
        return True
    if not (1 <= month <= 12):
        return True
    now = datetime.now(timezone.utc)
    return (year, month) < (now.year, now.month)


def _fetch_wayback_snapshot(
    url: str,
    *,
    timestamp_yyyymm: str | None = None,
    signature_check: bytes | None | str = "auto",
    retries: int = 3,
    timeout: int = 30,
    log: Callable[[str], None] | None = None,
) -> bytes | None:
    now_ym = datetime.now(timezone.utc).strftime("%Y%m")
    timestamp = (timestamp_yyyymm or now_ym).replace("-", "")
    if not _WAYBACK_TS_RE.fullmatch(timestamp):
        if log:
            log(f"WARN invalid wayback timestamp {timestamp_yyyymm!r}; using today")
        timestamp = now_ym
    api_url = (
        f"https://archive.org/wayback/available?url={requests.utils.quote(url, safe=':/')}"
        f"&timestamp={timestamp}"
    )
    if log:
        log(f"INFO wayback query: {api_url}")
    try:
        api_resp = fetch_text(api_url, retries=retries, timeout=timeout)
    except Exception as exc:
        if log:
            log(f"WARN wayback API request failed for {url}: {exc}")
        return None
    if not api_resp:
        if log:
            log(f"WARN wayback API empty response for {url}")
        return None
    try:
        payload = json.loads(api_resp)
    except json.JSONDecodeError as exc:
        if log:
            log(f"WARN wayback API JSON parse failed for {url}: {exc}")
        return None
    snapshot = payload.get("archived_snapshots", {}).get("closest", {})
    snapshot_url = snapshot.get("url")
    snapshot_ts = snapshot.get("timestamp")
    if not snapshot_url:
        if log:
            log(f"WARN wayback has no snapshot for {url}")
        return None
    if log:
        log(f"INFO wayback snapshot ts={snapshot_ts} url={snapshot_url}")
    try:
        snap_data = fetch_bytes(snapshot_url, retries=retries, timeout=timeout)
    except Exception as exc:
        if log:
            log(f"WARN wayback fetch failed for {snapshot_url}: {exc}")
        return None
    if _matches_signature(snap_data, url, signature_check):
        return snap_data
    if log:
        log(f"WARN wayback snapshot failed signature check for {snapshot_url}")
    return None


def fetch_with_wayback_fallback(
    url: str,
    *,
    timestamp_yyyymm: str | None = None,
    signature_check: bytes | None | str = "auto",
    retries: int = 3,
    timeout: int = 30,
    log: Callable[[str], None] | None = None,
) -> bytes | None:
    """Fetch ``url`` directly, falling back to the Wayback Machine on failure."""
    live_data: bytes | None = None
    try:
        live_data = fetch_bytes(url, retries=retries, timeout=timeout)
    except Exception as exc:
        if log:
            log(f"WARN live fetch failed for {url}: {exc}; trying Wayback")
        live_data = None
    if _matches_signature(live_data, url, signature_check):
        return live_data
    if live_data is not None and signature_check is not None and log:
        log(f"WARN live fetch returned wrong signature for {url}; trying Wayback")

    if not _should_try_wayback(timestamp_yyyymm):
        if log:
            log(f"INFO skipping Wayback lookup for current/future period {timestamp_yyyymm}")
        return None
    return _fetch_wayback_snapshot(
        url,
        timestamp_yyyymm=timestamp_yyyymm,
        signature_check=signature_check,
        retries=retries,
        timeout=timeout,
        log=log,
    )


_ORIG_FETCH_WAYBACK = fetch_with_wayback_fallback


def _accept_probe_payload(
    data: bytes | None,
    url: str,
    *,
    signature_check: bytes | None | str,
    convert_xls: bool,
    log: Callable[[str], None] | None,
) -> bytes | None:
    if not data:
        return None
    if convert_xls:
        if data.startswith(b"\xd0\xcf\x11\xe0"):
            if log:
                log(f"INFO downloaded legacy XLS from {url}; converting to XLSX")
            return ensure_xlsx(data, log=log)
        if not data.startswith(b"PK"):
            if log:
                log(f"WARN {url} returned non-Excel content; skipping")
            return None
    if not _matches_signature(data, url, signature_check):
        if log:
            log(f"WARN {url} failed signature check")
        return None
    return data


def _record_probe(
    report: "DiscoveryReport | None",
    report_key: str | None,
    url: str,
    *,
    status: str,
    reason: str,
) -> None:
    if report is not None and report_key is not None:
        report.add_probe(report_key, url, status=status, reason=reason)


def probe_url_variants(
    templates: list[str],
    format_kwargs: dict[str, str],
    *,
    signature_check: bytes | None | str = "auto",
    wayback_timestamp: str | None = None,
    convert_xls: bool = False,
    log: Callable[[str], None] | None = None,
    report: "DiscoveryReport | None" = None,
    report_key: str | None = None,
    matched_url: list[str] | None = None,
) -> bytes | None:
    """Try multiple URL templates live first, then Wayback fallback; return first valid response."""
    urls = list(dict.fromkeys(template.format(**format_kwargs) for template in templates))
    eff_sig = None if convert_xls else signature_check

    # Honor monkeypatching of fetch_with_wayback_fallback in tests.
    if fetch_with_wayback_fallback is not _ORIG_FETCH_WAYBACK:
        for url in urls:
            try:
                data = fetch_with_wayback_fallback(
                    url,
                    timestamp_yyyymm=wayback_timestamp,
                    signature_check=eff_sig,
                    log=log,
                )
            except Exception as exc:
                if log:
                    log(f"WARN fetch error for {url}: {exc}")
                _record_probe(report, report_key, url, status="error", reason=str(exc))
                continue
            accepted = _accept_probe_payload(
                data, url, signature_check=signature_check, convert_xls=convert_xls, log=log
            )
            _record_probe(
                report,
                report_key,
                url,
                status="ok" if accepted else "fail",
                reason="downloaded" if accepted else "fetch-failed",
            )
            if accepted is not None:
                if matched_url is not None:
                    matched_url[:] = [url]
                if log:
                    log(f"INFO downloaded from {url}")
                return accepted
        return None

    # Pass 1: try all live URLs before hitting Wayback.
    try_wayback = _should_try_wayback(wayback_timestamp)
    live_failures: dict[str, tuple[str, str]] = {}
    for url in urls:
        try:
            raw = fetch_bytes(url)
        except Exception as exc:
            if log:
                log(f"WARN live fetch failed for {url}: {exc}")
            live_failures[url] = ("error", str(exc))
            continue
        accepted = _accept_probe_payload(
            raw, url, signature_check=signature_check, convert_xls=convert_xls, log=log
        )
        if accepted is not None:
            _record_probe(report, report_key, url, status="ok", reason="downloaded")
            if matched_url is not None:
                matched_url[:] = [url]
            if log:
                log(f"INFO downloaded from {url}")
            return accepted
        live_failures[url] = ("fail", "fetch-failed")

    if not try_wayback:
        for url in urls:
            status, reason = live_failures.get(url, ("fail", "fetch-failed"))
            _record_probe(report, report_key, url, status=status, reason=reason)
        if log and wayback_timestamp:
            log(f"INFO skipping Wayback lookup for current/future period {wayback_timestamp}")
        return None

    # Pass 2: Wayback fallback across all candidate URLs.
    for url in urls:
        snap = _fetch_wayback_snapshot(
            url,
            timestamp_yyyymm=wayback_timestamp,
            signature_check=eff_sig,
            log=log,
        )
        accepted = _accept_probe_payload(
            snap, url, signature_check=signature_check, convert_xls=convert_xls, log=log
        )
        if accepted is not None:
            _record_probe(report, report_key, url, status="ok", reason="wayback")
            if matched_url is not None:
                matched_url[:] = [url]
            if log:
                log(f"INFO downloaded from Wayback for {url}")
            return accepted
        status, reason = live_failures.get(url, ("fail", "fetch-failed"))
        _record_probe(report, report_key, url, status=status, reason=reason)
    return None
