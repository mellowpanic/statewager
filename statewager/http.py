"""HTTP client helpers with retry/backoff, Wayback fallback, and URL probing."""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
import random
import re
import threading
import time
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse
import weakref

import requests
from requests.adapters import HTTPAdapter

from .parse import PDF_MAGIC, XLS_BIFF_MAGIC, XLS_SIGNATURES, XLSX_MAGIC, ensure_xlsx

if TYPE_CHECKING:
    from .discover import DiscoveryReport

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_THREAD_LOCAL = threading.local()
_SESSION_LOCK = threading.Lock()
_CREATED_SESSIONS: weakref.WeakSet[requests.Session] = weakref.WeakSet()


def _close_all_sessions() -> None:
    with _SESSION_LOCK:
        sessions = list(_CREATED_SESSIONS)
        _CREATED_SESSIONS.clear()
    for sess in sessions:
        try:
            sess.close()
        except Exception:
            pass


atexit.register(_close_all_sessions)


_MAX_RETRY_AFTER_SECONDS = 60.0
_MAX_RESPONSE_BYTES = 100 * 1024 * 1024


def _is_disallowed_host(hostname: str) -> bool:
    host = hostname.strip(".").lower()
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _validate_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Unsupported or invalid URL: {url!r}")
    if _is_disallowed_host(parsed.hostname):
        raise ValueError(f"Blocked private or local URL host: {url!r}")


class _SafeSession(requests.Session):
    """Session enforcing SSRF and scheme checks before following redirects."""

    def resolve_redirects(self, resp, req, **kwargs):  # type: ignore[override]
        for next_req in super().resolve_redirects(resp, req, **kwargs):
            if next_req.url:
                _validate_http_url(next_req.url)
            yield next_req


def _get_session() -> requests.Session:
    """Return a thread-local pooled HTTP session."""
    session = getattr(_THREAD_LOCAL, "session", None)
    if session is None:
        session = _SafeSession()
        session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=16)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _THREAD_LOCAL.session = session
        weakref.finalize(session, session.close)
        with _SESSION_LOCK:
            _CREATED_SESSIONS.add(session)
    return session


def _resolve_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        pass
    else:
        if not math.isfinite(delay):
            return None
        return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, delay))
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    delay = retry_at.timestamp() - time.time()
    if not math.isfinite(delay):
        return None
    return min(_MAX_RETRY_AFTER_SECONDS, max(0.0, delay))


def _sleep_with_jitter(base_seconds: float, *, jitter_max: float = 0.35) -> None:
    safe_base = max(0.0, base_seconds) if math.isfinite(base_seconds) else 0.0
    time.sleep(safe_base + random.uniform(0.0, jitter_max))


def retry_request(
    method: str,
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    data: bytes | str | dict[str, object] | None = None,
    stream: bool = False,
) -> requests.Response:
    """HTTP request with retry/backoff for transient failures."""
    _validate_http_url(url)
    client = session if session is not None else _get_session()
    attempts = max(1, retries)
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req_kwargs: dict[str, object] = {
                "timeout": timeout,
                "headers": headers,
                "data": data,
            }
            if stream:
                req_kwargs["stream"] = True
            response = client.request(method, url, **req_kwargs)
            if response.status_code in {429, 503} and attempt < attempts:
                retry_after = _resolve_retry_after(response.headers.get("Retry-After"))
                response.close()
                backoff = retry_after if retry_after is not None else max(1.0, attempt * 2.0)
                _sleep_with_jitter(backoff)
                continue
            if response.status_code == 404:
                return response
            if (500 <= response.status_code < 600 or response.status_code == 408) and attempt < attempts:
                response.close()
                _sleep_with_jitter(float(attempt))
                continue
            if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
                err = requests.HTTPError(
                    f"{response.status_code} Client Error for url: {url}",
                    response=response,
                )
                response.close()
                raise err
            try:
                response.raise_for_status()
            except Exception:
                response.close()
                raise
            return response
        except (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema):
            raise
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int) and 400 <= status < 500 and status not in {408, 429}:
                raise
            last_exc = exc
            if attempt < attempts:
                _sleep_with_jitter(float(attempt))
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts:
                _sleep_with_jitter(float(attempt))
    raise RuntimeError(f"Failed to {method.upper()} {url} after {attempts} attempts") from last_exc


def retry_get(
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    stream: bool = False,
) -> requests.Response:
    """HTTP GET with retry loop. Returns response on success and raises on final failure."""
    return retry_request(
        "GET",
        url,
        retries=retries,
        timeout=timeout,
        headers=headers,
        session=session,
        stream=stream,
    )


def _read_bounded_bytes(response: requests.Response, max_bytes: int) -> bytes:
    headers = getattr(response, "headers", None)
    if headers:
        raw_len = headers.get("Content-Length")
        if raw_len is not None:
            try:
                content_len = int(raw_len)
            except (TypeError, ValueError):
                content_len = -1
            if content_len > max_bytes:
                raise ValueError(f"HTTP response Content-Length {content_len} exceeds limit {max_bytes}")
    iter_fn = getattr(response, "iter_content", None)
    if callable(iter_fn):
        chunks = bytearray()
        for chunk in iter_fn(chunk_size=65536):
            if chunk:
                chunks.extend(chunk)
                if len(chunks) > max_bytes:
                    raise ValueError(f"HTTP response body exceeds limit of {max_bytes} bytes")
        return bytes(chunks)
    data = response.content
    if data is not None and len(data) > max_bytes:
        raise ValueError(f"HTTP response body exceeds limit of {max_bytes} bytes")
    return data


def fetch_bytes(
    url: str,
    retries: int = 3,
    timeout: int = 30,
    *,
    headers: dict[str, str] | None = None,
    session: requests.Session | None = None,
    max_bytes: int = _MAX_RESPONSE_BYTES,
) -> bytes | None:
    """HTTP GET returning bytes, or None on 404. Retries 429/5xx/timeouts."""
    response = retry_get(
        url, retries=retries, timeout=timeout, headers=headers, session=session, stream=True,
    )
    try:
        if response.status_code == 404:
            return None
        return _read_bounded_bytes(response, max_bytes)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


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


_XLS_SIGNATURES = XLS_SIGNATURES
_HTML_PREFIXES = (b"<!doctype", b"<html", b"<head", b"<body", b"<script", b"<!--")
_NON_DIGIT_RE = re.compile(r"[^0-9]")
_WAYBACK_TS_RE = re.compile(r"\d{4,14}")


def _signature_from_extension(url: str) -> bytes | None:
    lower = urlparse(url).path.lower()
    if lower.endswith(".pdf"):
        return PDF_MAGIC
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return XLSX_MAGIC
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
        if lower.endswith(".xls"):
            return data.startswith(XLS_SIGNATURES)
        if lower.endswith(".xlsx"):
            return data.startswith(XLSX_MAGIC)
        if lower.endswith(".pdf"):
            return data.startswith(PDF_MAGIC)
        if not lower.endswith((".html", ".htm")):
            head = data.lstrip()[:64].lower()
            if head.startswith(_HTML_PREFIXES):
                return False
        return True
    if isinstance(signature_check, (bytes, bytearray)):
        return data.startswith(bytes(signature_check))
    if signature_check is None:
        return True
    raise ValueError(f"Unsupported signature_check value: {signature_check!r}")


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
        return False
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
    timestamp = (timestamp_yyyymm or "").replace("-", "")
    if not timestamp or not _WAYBACK_TS_RE.fullmatch(timestamp):
        if timestamp_yyyymm and log:
            log(f"WARN invalid wayback timestamp {timestamp_yyyymm!r}; using today")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m")
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
    if not isinstance(payload, dict):
        return None
    snapshots = payload.get("archived_snapshots")
    closest = snapshots.get("closest") if isinstance(snapshots, dict) else None
    if not isinstance(closest, dict) or closest.get("available") is False:
        return None
    snapshot_url = closest.get("url")
    snapshot_ts = closest.get("timestamp")
    if not snapshot_url or not isinstance(snapshot_url, str):
        if log:
            log(f"WARN wayback has no snapshot for {url}")
        return None
    parsed_snap = urlparse(snapshot_url)
    snap_host = (parsed_snap.hostname or "").lower()
    if parsed_snap.scheme.lower() not in {"http", "https"} or (
        snap_host not in {"web.archive.org", "archive.org"} and not snap_host.endswith(".archive.org")
    ):
        if log:
            log(f"WARN rejecting untrusted wayback snapshot URL: {snapshot_url!r}")
        return None
    if parsed_snap.scheme.lower() == "http":
        snapshot_url = parsed_snap._replace(scheme="https").geturl()
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
        if data.startswith(XLS_BIFF_MAGIC):
            if log:
                log(f"INFO downloaded legacy XLS from {url}; converting to XLSX")
            return ensure_xlsx(data, log=log)
        if not data.startswith(XLSX_MAGIC):
            if log:
                log(f"WARN {url} returned non-Excel content; skipping")
            return None
        if signature_check in ("auto", XLSX_MAGIC, None):
            return data
    if not _matches_signature(data, url, signature_check):
        if log:
            log(f"WARN {url} failed signature check")
        return None
    return data


def _record_probe(
    report: DiscoveryReport | None,
    report_key: str | None,
    url: str,
    *,
    status: str,
    reason: str,
) -> None:
    if report is not None and report_key is not None:
        report.add_probe(report_key, url, status=status, reason=reason)


def _record_live_failure(
    report: DiscoveryReport | None,
    report_key: str | None,
    url: str,
    live_failures: dict[str, tuple[str, str]],
) -> None:
    status, reason = live_failures[url]
    _record_probe(report, report_key, url, status=status, reason=reason)


def _record_probe_hit(
    report: DiscoveryReport | None,
    report_key: str | None,
    url: str,
    matched_url: list[str] | None,
    reason: str,
    message: str,
    log: Callable[[str], None] | None,
) -> None:
    _record_probe(report, report_key, url, status="ok", reason=reason)
    if matched_url is not None:
        matched_url[:] = [url]
    if log:
        log(message)


def probe_url_variants(
    templates: list[str],
    format_kwargs: dict[str, str],
    *,
    signature_check: bytes | None | str = "auto",
    wayback_timestamp: str | None = None,
    convert_xls: bool = False,
    log: Callable[[str], None] | None = None,
    report: DiscoveryReport | None = None,
    report_key: str | None = None,
    matched_url: list[str] | None = None,
) -> bytes | None:
    """Try multiple URL templates live first, then Wayback fallback; return first valid response."""
    urls = list(dict.fromkeys(template.format(**format_kwargs) for template in templates))
    eff_sig = None if convert_xls else signature_check

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
            _record_probe_hit(
                report, report_key, url, matched_url, "downloaded", f"INFO downloaded from {url}", log
            )
            return accepted
        live_failures[url] = ("fail", "fetch-failed")

    if not try_wayback:
        for url in urls:
            _record_live_failure(report, report_key, url, live_failures)
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
            _record_probe_hit(
                report, report_key, url, matched_url, "wayback", f"INFO downloaded from Wayback for {url}", log
            )
            return accepted
        _record_live_failure(report, report_key, url, live_failures)
    return None
