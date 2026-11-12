"""HTTP client helpers with retry/backoff, Wayback fallback, and URL probing."""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
import json
import random
import re
import time
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

import requests

from .parse import ensure_xlsx

if TYPE_CHECKING:
    from .discover import DiscoveryReport


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
    client = session or requests
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


def _signature_from_extension(url: str) -> bytes | None:
    lower = urlparse(url).path.lower()
    if lower.endswith(".pdf"):
        return b"%PDF"
    if lower.endswith(".xlsx") or lower.endswith(".xls"):
        return b"PK"
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
    if signature_check == "auto":
        signature = _signature_from_extension(url)
    elif isinstance(signature_check, (bytes, bytearray)):
        signature = bytes(signature_check)
    else:
        signature = None

    def _good(data: bytes | None) -> bool:
        if data is None:
            return False
        if signature is None:
            return True
        return data.startswith(signature)

    live_data: bytes | None = None
    try:
        live_data = fetch_bytes(url, retries=retries, timeout=timeout)
    except Exception as exc:
        if log:
            log(f"WARN live fetch failed for {url}: {exc}; trying Wayback")
        live_data = None
    if _good(live_data):
        return live_data
    if live_data is not None and signature is not None and log:
        log(f"WARN live fetch returned wrong signature for {url}; trying Wayback")

    timestamp = (timestamp_yyyymm or datetime.utcnow().strftime("%Y%m")).replace("-", "")
    if not re.fullmatch(r"\d{4,14}", timestamp):
        if log:
            log(f"WARN invalid wayback timestamp {timestamp_yyyymm!r}; using today")
        timestamp = datetime.utcnow().strftime("%Y%m")
    api_url = (
        f"http://archive.org/wayback/available?url={requests.utils.quote(url, safe=':/?&=%')}"
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
    snapshot = (
        payload.get("archived_snapshots", {})
        .get("closest", {})
    )
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
    if _good(snap_data):
        return snap_data
    if log:
        log(f"WARN wayback snapshot failed signature check for {snapshot_url}")
    return None


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
) -> bytes | None:
    """Try multiple URL templates with Wayback fallback, return first valid response."""
    for template in templates:
        url = template.format(**format_kwargs)
        try:
            data = fetch_with_wayback_fallback(
                url,
                timestamp_yyyymm=wayback_timestamp,
                signature_check=None if convert_xls else signature_check,
                log=log,
            )
        except Exception as exc:
            if log:
                log(f"WARN fetch error for {url}: {exc}")
            if report is not None and report_key is not None:
                report.add_probe(report_key, url, status="error", reason=str(exc))
            continue
        if report is not None and report_key is not None:
            report.add_probe(
                report_key, url,
                status="ok" if data else "fail",
                reason="downloaded" if data else "fetch-failed",
            )
        if data is None:
            continue
        if convert_xls:
            if data.startswith(b"\xd0\xcf\x11\xe0"):
                if log:
                    log(f"INFO downloaded legacy XLS from {url}; converting to XLSX")
                converted = ensure_xlsx(data, log=log)
                if converted is not None:
                    return converted
                continue
            if not data.startswith(b"PK"):
                if log:
                    log(f"WARN {url} returned non-Excel content; skipping")
                continue
        if signature_check == "auto":
            sig = _signature_from_extension(url)
            if sig and not data.startswith(sig):
                if log:
                    log(f"WARN {url} failed signature check")
                continue
        elif isinstance(signature_check, (bytes, bytearray)):
            if not data.startswith(signature_check):
                if log:
                    log(f"WARN {url} failed signature check")
                continue
        if log:
            log(f"INFO downloaded from {url}")
        return data
    return None
